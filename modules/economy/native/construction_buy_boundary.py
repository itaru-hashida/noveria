"""Граница строительных buy orders: одна привязка реализации на импорт.

Модуль не хранит состояния движка и не создаёт второго order/project/stock
state: ``account`` и ``project`` — живые словари движка, ``sells`` — та же
книга, а заявку создаёт существующий ``V3.order``. Копий заявок и счетов нет;
``CashStore`` и ``OrderStore`` остаются единственными владельцами денег и
заявок.

Что перенесено в ядро (``construction_buy_kernel``):

  * расчёт buy price/ceiling — ``quoted_cost`` даёт тройку
    ``(value, ceiling, exact)``; прежнее тело брало второй элемент, и ядро
    берёт его же;
  * создание construction buy order для недостающего материала — через
    существующий hook ``V3.order``.

Что осталось в Python и почему:

  * недостающий объём (``missing``) и количество закупки считает нативный
    ``material_kernel.reserve_materials``: он обходит смету в порядке
    ``cfg['construction']``, закрепляет склад за проектом и зовёт хук на каждый
    материал, включая ``missing == 0``. Здесь объём не пересчитывается, иначе
    появился бы второй источник истины для количества;
  * ``quoted_cost`` и ``order`` — хуки движка: книга, кэш котировок,
    ``OrderStore.append``, ``CashStore.add_reserved_cash``, ревизия спроса и
    отчёт остаются у движка;
  * денежное урезание закупки (``min(quantity, free_cash/price)``), расчёт
    ``order['reserved']`` и запись резерва живут в ``V3.order``: второй копии
    денежной политики здесь нет;
  * ``project['materials']`` и ``stock_reserved`` уже обновлены
    ``material_kernel`` до этого вызова, поэтому граница их не трогает;
  * dirty goods прежний участок не помечал: buy-ветка ``V3.order`` двигает
    ``_demand_revision``, а не котировки. Новых пометок граница не добавляет;
  * RNG, очередь инвестиций и отчёт граница не затрагивает вовсе; rollback не
    добавляется.

Привязка к прежнему call site. Ядро резерва получает ``place_order`` хуком и
вызывает его с сигнатурой ``(owner, account, good, missing, sells)`` — ровно
такой, как у прежнего ``material_boundary.place_order_hook``. Чтобы прежний
call site ``delivery_boundary.request_project_materials`` не пришлось менять
(эти файлы вне разрешённого участка правки), модуль один раз при импорте
подставляет ``place_order_hook`` в ``material_boundary``: граница доставки
разрешает это имя при каждом вызове, поэтому подстановка видна сразу. Прежняя
реализация сохраняется в ``PYTHON_PLACE_ORDER_HOOK``, а ``install_hook`` /
``restore_hook`` позволяют вернуть её явно. Второго цикла и второй записи
материала подстановка не создаёт — тело хука остаётся тем же участком.

Эталонная Python-реализация живёт в этом же модуле: она нужна как образец для
focused-теста на побитовое совпадение и как fallback, если расширение не
собрано. Реализации принимают одни и те же аргументы, поэтому тест гоняет обе
на одинаковом состоянии и сравнивает значения, типы и порядок вызовов хуков.

Модуль не импортирует ``modules.economy.support``: EPS приходит аргументом,
поэтому циклического импорта нет.
"""

import importlib

from . import material_boundary

# Назначение строительной закупки. Строка берётся у прежнего участка, поэтому
# подменить литерал незаметно нельзя: по нему ``project_cash_reserve`` и
# ``trade_kernel`` узнают материалы стройки.
PURPOSE = material_boundary.MATERIAL_PURPOSE
# Сторона заявки: прежний литерал.
SIDE = 'buy'


def quoted_cost_hook(owner, account, good, missing, sells):
    """Хук котировки: тело остаётся у движка (``V3.quoted_cost``)."""
    return owner.quoted_cost(account, good, missing, sells)


def order_hook(owner, account_id, good, quantity, side, price, purpose):
    """Хук заявки: тело остаётся у движка (``V3.order``)."""
    return owner.order(account_id, good, quantity, side, price, purpose)


def place_order_python(owner, account, good, missing, sells, purpose, quote, order):
    """Эталон прежнего участка: котировка, распаковка тройки, заявка.

    Аргументы повторяют native-ядро один в один, чтобы focused-тест мог
    прогнать обе реализации на одинаковом состоянии и сравнить побитово.
    ``value`` и ``exact`` прежний код не использовал — второй элемент тройки
    (``ceiling``) становится ценой закупки.
    """
    _, price, _ = quote(owner, account, good, missing, sells)
    order(owner, account['id'], good, missing, SIDE, price, purpose)
    return None


def place_order_hook(owner, account, good, missing, sells):
    """Прежняя сигнатура хука: тот же участок, но телом ядра.

    Тонкая привязка нужна потому, что ядро резерва зовёт хук с пятью
    аргументами, а ядру закупки нужны ещё назначение и хуки движка. Второго
    участка здесь нет: вся работа — один вызов ``place_order``.
    """
    return place_order(owner, account, good, missing, sells, PURPOSE,
                       quoted_cost_hook, order_hook)


def load_construction_buy_kernel():
    """Вернуть native-модуль construction_buy_kernel или None, если не собран.

    Отсутствие расширения даёт ImportError, а не ModuleNotFoundError: пакет
    ``modules.economy.native`` существует, а подмодуля нет. Обе ситуации
    означают одно — ядро не собрано, берём эталонную Python-ветку.
    """
    try:
        return importlib.import_module('.construction_buy_kernel', __package__)
    except (ImportError, ModuleNotFoundError):
        return None


NATIVE_CONSTRUCTION_BUY_KERNEL = False

_kernel = load_construction_buy_kernel()
if _kernel is not None:
    place_order = _kernel.place_order
    NATIVE_CONSTRUCTION_BUY_KERNEL = True
else:
    place_order = place_order_python

# Прежняя реализация хука сохраняется, чтобы подстановку можно было вернуть.
PYTHON_PLACE_ORDER_HOOK = material_boundary.place_order_hook


def install_hook():
    """Подставить нативную реализацию в прежний call site резерва."""
    material_boundary.place_order_hook = place_order_hook
    return place_order_hook


def restore_hook():
    """Вернуть прежнюю реализацию хука (для тестов и диагностики)."""
    material_boundary.place_order_hook = PYTHON_PLACE_ORDER_HOOK
    return PYTHON_PLACE_ORDER_HOOK


# Одна подстановка на импорт: имя привязывается, а не диспетчеризуется на
# каждом материале.
install_hook()
