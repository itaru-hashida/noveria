"""Границы отмены проекта: простой стройки, маржа и устойчивое отсутствие спроса.

Модуль не хранит состояние движка и не создаёт второго project state:
``account`` и ``project`` — живые словари движка, поэтому
``project['last_progress']``/``project['progress_quantity']``, которые меняет
ядро, — те же поля, что читает остальной движок. Отмена по марже/спросу
вообще не пишет проект: ядро только читает его и возвращает результат.

Что перенесено в ядро (``cancel_kernel``) для stall-среза:

  * прогресс стройки — ``sum`` по значениям ``project['materials']``;
  * проверка порога простоя ``month-last_progress >= project_stall_months``;
  * определение причины отмены (``STALL_REASON``);
  * подготовка результата отмены — ``dict(outcome='cancelled', reason=...)``.

Что перенесено в ядро для среза маржи/спроса:

  * расчёт порога маржи — ``margin < -eps`` в прежнем порядке арифметики;
  * расчёт порога отсутствия спроса — ``absent`` коротким замыканием
    (история рынка, возраст проекта, ``volume``, ``unmet``,
    ``reachable_demand``);
  * проверка условия отмены ``due and (margin < -eps or absent)``: маржа
    проверяется первой, поэтому её приоритет сохранён;
  * формирование причины и результата отмены (``MARGIN_DEMAND_REASON``).

Что осталось в Python и почему:

  * выбор проекта и порядок проектов — цикл ``v3.project_material_requests``
    по перемешанному списку инвесторов; ядро получает уже выбранный проект;
  * снятие проекта со счёта, восстановление ``protected`` и запись
    ``report['investment_decisions']`` — хук ``apply_cancellation_hook``:
    денежная политика и отчёт остаются у движка, ядро задаёт только момент
    вызова и аргументы, а значение ``protected`` пишет ``CashStore``. Оба
    среза применяют отмену одним и тем же хуком, поэтому side effects прежнего
    пути не меняются;
  * цена, себестоимость, рыночный сигнал и ``reachable_demand`` — хуки
    движка: это большие методы с кэшами, ядро зовёт их в прежнем порядке;
  * ``cancel_order``, освобождение sell-заявок, ``mark_market_quotes_dirty``,
    dirty goods, события и очередь в этих срезах не вызываются: прежние пути
    их не трогали, и граница не добавляет новых side effects. Освобождение
    собственных sell-заявок и их отмена живут в ``material_boundary`` и
    выполняются позже, только для не отменённых проектов;
  * доставка, смета, резерв материалов и строительные закупки не
    затрагиваются вовсе; rollback не добавляется.

Эталонная Python-реализация живёт в этом же модуле: она нужна как образец для
focused-теста на побитовое совпадение и как fallback, если расширение не
собрано. Реализации принимают одни и те же аргументы, поэтому тест гоняет обе
на одинаковом состоянии и сравнивает значения, типы и порядок вызовов хуков.

Модуль не импортирует ``modules.economy.support``: EPS приходит аргументом,
поэтому циклического импорта нет.
"""

import builtins
import importlib

from . import cash_boundary, project_slot
from .phase_contracts import CANCELLATION_RESULT_FIELDS, require_fields

# Встроенный sum: float-слагаемые CPython 3.12 складывает компенсированно,
# поэтому прогресс обязан считаться той же функцией, что и прежде.
_PY_SUM = builtins.sum

# Причина stall-отмены: тот же текст, что и в прежнем теле v3.
STALL_REASON = 'Стройка не продвигается'

# Причина отмены по марже/спросу: прежний литерал v3. У обоих условий
# (отрицательная маржа и устойчивое отсутствие спроса) причина одна.
MARGIN_DEMAND_REASON = 'Отрицательная маржа или устойчивое отсутствие спроса'

# Порог наблюдаемости отсутствия спроса — прежние литералы тела v3.
ABSENT_MIN_HISTORY = 3
ABSENT_MIN_AGE = 3


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
        project_slot.set_field(project, 'last_progress', month)
    project_slot.set_field(project, 'progress_quantity', progress)
    project.setdefault('last_progress', project['created'])
    return month-project['last_progress'] >= stall_months


def cancellation_result_python(reason):
    """Эталон результата отмены: те же поля в том же порядке."""
    return dict(outcome='cancelled', reason=reason)


def net_price_hook(owner, e):
    """Хук цены: тело остаётся у движка (``V3.net_price``)."""
    return owner.net_price(e)


def unit_cost_hook(owner, e, sells):
    """Хук себестоимости: тело остаётся у движка (``V3.unit_cost``)."""
    return owner.unit_cost(e, sells)


def market_signal_hook(owner, history, state, province, good):
    """Хук рыночного сигнала: тело остаётся у движка (``market_signal``)."""
    return owner.market_signal(history, state, province, good)


def reachable_demand_hook(owner, e, sells):
    """Хук доступного спроса: тело остаётся у движка (``reachable_demand``)."""
    return owner.reachable_demand(e, sells)


def check_margin_demand_python(owner, account, project, sells, due, eps,
                               net_price, unit_cost, market_signal,
                               reachable_demand):
    """Эталон среза маржи/спроса: прежнее тело ``project_material_requests``.

    Порядок прежний: ``e`` и ``margin`` всегда, затем рыночный сигнал, затем
    ``absent`` коротким замыканием, затем условие отмены. Возвращает результат
    отмены или ``None``.
    """
    good = project['good']
    loc = project['location']
    e = dict(id=account['id'], good=good, price=project['expected_price'], **loc)
    margin = (net_price(owner, e)-unit_cost(owner, e, sells)
              -owner.goods[good]['labor']*owner.cfg['wage']
              *owner.cfg.get('enterprise_overhead_rate', 0))
    history = owner.meta.get('market_history', [])
    signal = market_signal(owner, history, account['state_id'],
                           loc['province_id'], good)
    absent = (len(history) >= ABSENT_MIN_HISTORY
              and owner.meta['month']-project['created'] >= ABSENT_MIN_AGE
              and signal['volume'] <= eps and signal['unmet'] <= eps
              and reachable_demand(owner, e, sells) <= eps)
    if due and (margin < -eps or absent):
        return cancellation_result_python(MARGIN_DEMAND_REASON)
    return None


def cancel_margin_demand_project_python(owner, account, sells, due, eps,
                                        apply_cancellation=None):
    """Эталон порядка: проверка маржи/спроса -> применение отмены."""
    apply_cancellation = apply_cancellation or apply_cancellation_hook
    result = check_margin_demand_python(
        owner, account, account['project'], sells, due, eps, net_price_hook,
        unit_cost_hook, market_signal_hook, reachable_demand_hook)
    if result is None:
        return False
    apply_cancellation(owner, account, MARGIN_DEMAND_REASON, result)
    return True


def apply_cancellation_hook(owner, account, reason, result):
    """Хук применения отмены: снять проект, вернуть защиту, записать отчёт.

    Тело повторяет прежние три операции прежним порядком: ``del project``,
    восстановление ``protected`` и запись решения в отчёт. ``result`` уже
    подготовлен ядром, поэтому ключи решения идут прежним порядком
    ``kind``/``investor``/``outcome``/``reason``. Восстановленную защиту пишет
    ``CashStore``: это его денежная колонка, а не поле представления.
    """
    require_fields(result, CANCELLATION_RESULT_FIELDS, contract='cancellation')
    project_slot.detach(account)
    cash_boundary.store_for(owner, account).set_protected_cash(
        account['id'],
        account.get('operating_protected', 0)+account.get('payroll_protected', 0))
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


def cancel_margin_demand_project(owner, account, sells, due, eps,
                                 apply_cancellation=None):
    """Отменить проект по марже/спросу или подтвердить, что отмены нет.

    ``True`` означает, что проект снят со счёта: ядро посчитало пороги маржи и
    отсутствия спроса, проверило условие и определило причину, а хук применил
    отмену. ``False`` — проект остался, и v3 продолжает прежний путь доставки.

    Хуки цены, себестоимости, сигнала и спроса передаются ядру явно: тела
    остаются у движка, поэтому кэши заполняются ровно там же, где прежде.
    """
    apply_cancellation = apply_cancellation or apply_cancellation_hook
    project = account.get('project')
    if not project:
        return False
    result = check_margin_demand(
        owner, account, project, sells, due, eps, net_price_hook, unit_cost_hook,
        market_signal_hook, reachable_demand_hook)
    if result is None:
        return False
    apply_cancellation(owner, account, MARGIN_DEMAND_REASON, result)
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
    check_margin_demand = _kernel.check_margin_demand
    NATIVE_CANCEL_KERNEL = True
else:
    check_stall = check_stall_python
    cancellation_result = cancellation_result_python
    project_progress = project_progress_python
    check_margin_demand = check_margin_demand_python
