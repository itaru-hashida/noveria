"""Граница stall-отмены проекта: одна привязка реализации на импорт.

Модуль не хранит состояние движка и не создаёт второго project state:
``account`` и ``project`` — живые словари движка, поэтому
``project['last_progress']``/``project['progress_quantity']``, которые меняет
ядро, — те же поля, что читает остальной движок.

Что перенесено в ядро (``cancel_kernel``):

  * прогресс стройки — ``sum`` по значениям ``project['materials']``;
  * проверка порога простоя ``month-last_progress >= project_stall_months``;
  * определение причины отмены (``STALL_REASON``);
  * подготовка результата отмены — ``dict(outcome='cancelled', reason=...)``.

Что осталось в Python и почему:

  * выбор проекта и порядок проектов — цикл ``v3.project_material_requests``
    по перемешанному списку инвесторов; ядро получает уже выбранный проект;
  * снятие проекта со счёта, восстановление ``protected`` и запись
    ``report['investment_decisions']`` — хук ``apply_cancellation_hook``:
    денежная политика и отчёт остаются у движка, ядро задаёт только момент
    вызова и аргументы;
  * ``cancel_order``, освобождение sell-заявок, ``mark_market_quotes_dirty``,
    dirty goods, события и очередь в этом срезе не вызываются: прежний
    stall-путь их не трогал, и граница не добавляет новых side effects.
    Освобождение собственных sell-заявок и их отмена живут в
    ``material_boundary`` и выполняются позже, только для не отменённых
    проектов;
  * отмена по марже/спросу, доставка, смета, резерв материалов и строительные
    закупки не затрагиваются вовсе; rollback не добавляется.

Эталонная Python-реализация живёт в этом же модуле: она нужна как образец для
focused-теста на побитовое совпадение и как fallback, если расширение не
собрано. Реализации принимают одни и те же аргументы, поэтому тест гоняет обе
на одинаковом состоянии и сравнивает значения, типы и порядок вызовов хуков.

Модуль не импортирует ``modules.economy.support``: EPS приходит аргументом,
поэтому циклического импорта нет.
"""

import builtins
import importlib

# Встроенный sum: float-слагаемые CPython 3.12 складывает компенсированно,
# поэтому прогресс обязан считаться той же функцией, что и прежде.
_PY_SUM = builtins.sum

# Причина stall-отмены: тот же текст, что и в прежнем теле v3.
STALL_REASON = 'Стройка не продвигается'


def project_progress_python(project):
    """Эталон прогресса: ``sum`` по значениям материалов проекта."""
    return _PY_SUM(project['materials'].values())


def check_stall_python(project, month, stall_months, eps):
    """Эталон проверки простоя: запись progress-state и решение об отмене.

    Порядок прежний: сначала сравнение с прошлым прогрессом, затем запись
    ``progress_quantity``, затем ``setdefault('last_progress', ...)`` и только
    потом проверка порога.
    """
    progress = _PY_SUM(project['materials'].values())
    if progress > project.get('progress_quantity', 0)+eps:
        project['last_progress'] = month
    project['progress_quantity'] = progress
    project.setdefault('last_progress', project['created'])
    return month-project['last_progress'] >= stall_months


def cancellation_result_python(reason):
    """Эталон результата отмены: те же поля в том же порядке."""
    return dict(outcome='cancelled', reason=reason)


def apply_cancellation_hook(owner, account, reason, result):
    """Хук применения отмены: снять проект, вернуть защиту, записать отчёт.

    Тело повторяет прежние три операции прежним порядком: ``del project``,
    восстановление ``protected`` и запись решения в отчёт. ``result`` уже
    подготовлен ядром, поэтому ключи решения идут прежним порядком
    ``kind``/``investor``/``outcome``/``reason``.
    """
    del account['project']
    account['protected'] = (account.get('operating_protected', 0)
                            +account.get('payroll_protected', 0))
    owner.report['investment_decisions'].append(
        dict(kind='investment_decision', investor=account['id'], **result))
    return None


def cancel_stalled_project_python(owner, account, eps, apply_cancellation=None):
    """Эталон порядка: проверка простоя -> применение отмены."""
    apply_cancellation = apply_cancellation or apply_cancellation_hook
    project = account.get('project')
    if not project:
        return False
    if not check_stall_python(project, owner.meta['month'],
                              owner.cfg['project_stall_months'], eps):
        return False
    apply_cancellation(owner, account, STALL_REASON,
                       cancellation_result_python(STALL_REASON))
    return True


def cancel_stalled_project(owner, account, eps, apply_cancellation=None):
    """Отменить проект по простою или подтвердить, что отмены нет.

    ``True`` означает, что проект снят со счёта: ядро обновило progress-state и
    определило причину, а хук применил отмену. ``False`` — проект остался, и
    v3 продолжает прежнюю ветку маржи/спроса.
    """
    apply_cancellation = apply_cancellation or apply_cancellation_hook
    project = account.get('project')
    if not project:
        return False
    if not check_stall(project, owner.meta['month'],
                       owner.cfg['project_stall_months'], eps):
        return False
    apply_cancellation(owner, account, STALL_REASON, cancellation_result(STALL_REASON))
    return True


def load_cancel_kernel():
    """Вернуть native-модуль cancel_kernel или None, если он не собран.

    Отсутствие расширения даёт ImportError, а не ModuleNotFoundError: пакет
    ``modules.economy.native`` существует, а подмодуля нет. Обе ситуации
    означают одно — ядро не собрано, берём эталонную Python-ветку.
    """
    try:
        return importlib.import_module('.cancel_kernel', __package__)
    except (ImportError, ModuleNotFoundError):
        return None


NATIVE_CANCEL_KERNEL = False

_kernel = load_cancel_kernel()
if _kernel is not None:
    check_stall = _kernel.check_stall
    cancellation_result = _kernel.cancellation_result
    project_progress = _kernel.project_progress
    NATIVE_CANCEL_KERNEL = True
else:
    check_stall = check_stall_python
    cancellation_result = cancellation_result_python
    project_progress = project_progress_python
