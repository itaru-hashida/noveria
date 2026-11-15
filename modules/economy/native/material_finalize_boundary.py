"""Граница финализации material request: одна привязка реализации на импорт.

Модуль закрывает последний участок ``project_material_requests``: после того как
смета, доставка, резерв материалов и строительные закупки уже посчитаны
существующими срезами (``material_boundary``/``delivery_boundary``), а отмены
решены ``cancel_boundary``, нужно определить затронутые товары и собрать
готовые numeric diagnostics и journal-строки.

Второго material/project state модуль не создаёт. ``account`` и ``project`` —
живые словари движка, ``bill`` и ``project['delivery']`` — уже посчитанные
значения. Ядро ничего не мутирует: оно читает готовые числа и возвращает новый
dict, а Python-хук применяет результат к отчёту. Поэтому delivery, reservation,
cancellation и construction-buy остаются в своих срезах, и rollback не
добавляется.

Что остаётся в Python и почему:

  * порядок проектов — цикл ``v3.project_material_requests`` по перемешанному
    списку инвесторов: граница получает уже выбранный проект и его смету;
  * ``mark_market_quotes_dirty`` — расширяемый хук движка: он сбрасывает
    внешние engine caches, версии книг и epoch полной инвалидации. Ядро
    фиксирует только фактически выполненные пометки (их сделали release- и
    cancel-срезы), поэтому второй пометки не появляется;
  * ``_demand_revision`` — ревизия спроса: buy-ветка ``V3.order`` двигает её
    один раз на заявку, и ядро лишь читает значение, иначе ревизии сдвинулись
    бы дважды;
  * добавление в ``engine.report``, события, пользовательские callbacks и
    очередь — ``apply_rows_hook``: отчётная политика остаётся у движка, ядро
    задаёт только готовые строки и их порядок.

Эталонная Python-реализация живёт в этом же модуле: она нужна как образец для
focused-теста на побитовое совпадение и как fallback, если расширение не
собрано. Реализации принимают одни и те же аргументы, поэтому тест гоняет обе
на одном состоянии и сравнивает значения, типы и порядок строк.

Имена привязываются один раз при импорте, чтобы не платить за диспетчер на
каждом проекте. Модуль не импортирует ``modules.economy.support``: EPS приходит
аргументом, поэтому циклического импорта нет.
"""

import builtins
import importlib

# Встроенные max/sum: ``max(0, x)`` сохраняет тип аргумента (int-остаток не
# становится float), а ``sum`` 3.12 складывает float-слагаемые компенсированно.
# Эталон обязан повторить обе функции, иначе типы и последние биты разойдутся.
_PY_MAX = builtins.max
_PY_SUM = builtins.sum

# Поле материалов проекта: единственный источник доставленного и
# зарезервированного количества. Второго склада и второй копии нет.
MATERIAL_FIELD = 'materials'

# Ключ отчёта, в который Python-хук складывает готовые journal-строки. Прежний
# путь строк material request в отчёт не писал, поэтому ключ новый и не
# перетирает ни один существующий слой отчёта.
REPORT_KEY = 'material_requests'


def affected_goods_python(bill):
    """Эталон затронутых товаров: ключи сметы в порядке словаря."""
    return list(bill)


def project_id_python(account, project):
    """Эталон идентификатора проекта: ``project_id``, иначе счёт-владелец."""
    project_id = project.get('project_id')
    return account.get('id') if project_id is None else project_id


def progress_python(project, materials):
    """Эталон полей прогресса: тот же встроенный ``sum`` и те же ключи."""
    return dict(quantity=_PY_SUM(materials.values()),
                last_progress=project.get('last_progress'),
                created=project.get('created'))


def material_row_python(account, project, good, quantity, delivery, reason,
                        revisions):
    """Эталон одной journal-строки: те же поля в том же порядке."""
    materials = project.get(MATERIAL_FIELD, {})
    delivered = materials.get(good, 0)
    revision = 0 if revisions is None else revisions.get(good, 0)
    return dict(project_id=project_id_python(account, project),
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
        progress=progress_python(project, materials))


def material_rows_python(account, project, bill, delivery, reason=None,
                         revisions=None):
    """Эталон журнала по смете: строки в порядке ``bill``."""
    return [material_row_python(account, project, good, quantity, delivery,
                                reason, revisions)
            for good, quantity in bill.items()]


def finalize_material_request_python(account, project, bill, delivery, eps,
                                     reason=None, revisions=None,
                                     quote_versions=None):
    """Эталон финализации одного проекта: те же поля и тот же порядок."""
    rows = material_rows_python(account, project, bill, delivery, reason, revisions)
    goods = affected_goods_python(bill)
    return dict(project_id=project_id_python(account, project),
        goods=goods,
        dirty_goods=dirty_goods_python(goods, quote_versions),
        revisions=revision_rows_python(goods, revisions),
        rows=rows,
        delivery=delivery,
        reason=reason,
        requested_total=_total_python(rows, 'requested'),
        delivered_total=_total_python(rows, 'delivered'),
        reserved_total=_total_python(rows, 'reserved'),
        missing_total=_total_python(rows, 'missing'))


def dirty_goods_python(goods, quote_versions):
    """Эталон записи dirty-товаров: только те, где версия уже выросла."""
    if quote_versions is None:
        return []
    return [good for good in goods if quote_versions.get(good, 0) > 0]


def revision_rows_python(goods, revisions):
    """Эталон ревизий спроса по товарам сметы в прежнем порядке."""
    if revisions is None:
        return []
    return [dict(good=good, revision=revisions.get(good, 0)) for good in goods]


def finalize_material_requests_python(records, eps, revisions=None,
                                      quote_versions=None):
    """Эталон финализации всех проектов: тот же порядок проектов и строк."""
    projects = [finalize_material_request_python(
        record[0], record[1], record[2], record[3], eps, record[4],
        revisions, quote_versions) for record in records]
    rows = [row for project in projects for row in project['rows']]
    return dict(
        projects=projects,
        rows=rows,
        dirty_goods=[good for project in projects for good in project['dirty_goods']],
        goods=[good for project in projects for good in project['goods']],
        requested_total=_total_python(rows, 'requested'),
        delivered_total=_total_python(rows, 'delivered'),
        reserved_total=_total_python(rows, 'reserved'),
        missing_total=_total_python(rows, 'missing'))


def _total_python(rows, field):
    """Сумма поля журнала встроенным ``sum`` в порядке строк."""
    return _PY_SUM([row[field] for row in rows])


def apply_rows_hook(owner, result, key=REPORT_KEY):
    """Хук применения: добавить готовые строки в отчёт движка.

    Отчётная политика остаётся у движка: ядро отдаёт готовые dict-строки в
    прежнем порядке, а куда их положить и в каком detail-режиме — решает
    Python. При ``detail == 'none'`` отчёт намеренно не пополняется: этот
    режим, как и прежде, не хранит тяжёлые диагностические слои. Хук ничего не
    пересчитывает, поэтому каждая строка журнала добавляется один раз.
    """
    rows = result.get('rows') or []
    if not rows:
        return rows
    if owner.report_detail() == 'none':
        return []
    owner.report.setdefault(key, []).extend(dict(row) for row in rows)
    return rows


def apply_material_request(owner, account, project, bill, eps, reason=None):
    """Финализировать один проект и применить готовые строки хуком.

    Смета, доставка и резерв уже посчитаны вызывающим срезом, поэтому здесь
    только чтение готовых чисел: ядро не пересчитывает ни материал, ни деньги,
    ни заявки. Повторных пометок котировок и повторных ревизий нет — ядро
    читает уже выполненные обновления, а отчёт пополняет Python-хук один раз.
    """
    result = finalize_material_request(
        account, project, bill, project.get('delivery', 0), eps, reason,
        getattr(owner, '_demand_revision', None),
        getattr(owner, '_market_quote_versions', None))
    apply_rows_hook(owner, result)
    return result


def load_material_finalize_kernel():
    """Вернуть native-модуль material_finalize_kernel или None, если не собран.

    Отсутствие расширения даёт ImportError, а не ModuleNotFoundError: пакет
    ``modules.economy.native`` существует, а подмодуля нет. Обе ситуации
    означают одно — ядро не собрано, берём эталонную Python-ветку.
    """
    try:
        return importlib.import_module('.material_finalize_kernel', __package__)
    except (ImportError, ModuleNotFoundError):
        return None


NATIVE_MATERIAL_FINALIZE_KERNEL = False

_kernel = load_material_finalize_kernel()
if _kernel is not None:
    affected_goods = _kernel.affected_goods
    material_row = _kernel.material_row
    material_rows = _kernel.material_rows
    finalize_material_request = _kernel.finalize_material_request
    finalize_material_requests = _kernel.finalize_material_requests
    NATIVE_MATERIAL_FINALIZE_KERNEL = True
else:
    affected_goods = affected_goods_python
    material_row = material_row_python
    material_rows = material_rows_python
    finalize_material_request = finalize_material_request_python
    finalize_material_requests = finalize_material_requests_python
