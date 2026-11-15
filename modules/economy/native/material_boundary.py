"""Граница резерва строительных материалов: одна привязка реализации на импорт.

Модуль не хранит состояние движка и не создаёт второго склада: ``account`` —
живой словарь счёта, поэтому ``stock_reserved`` и ``project['materials']``,
которые меняет ядро, — те же вложенные словари, что читает остальной движок.
Копий заявок и счетов нет, а ``store`` — существующая граница мутаций заявок
(``order_boundary``), переданная вызовом, а не пересозданная здесь.

Что перенесено в ядро: расчёт сметы, проверка доступного склада, закрепление
материалов за проектом и обновление ``stock_reserved``.

Что осталось в Python и почему:

  * выбор проекта и порядок проектов — цикл ``v3.project_material_requests``
    по перемешанному списку инвесторов; ядро получает уже выбранный проект;
  * отмена по простою и по марже/спросу живёт целиком в v3 до этого вызова;
  * доставка и защита денег (``project['delivery']``, ``account['protected']``)
    живут в отдельной границе ``delivery_boundary``: она обязана выполниться
    **до** строительных закупок, потому что ``free_cash`` читает
    ``protected`` и без него ``order`` урезал бы заявку по деньгам;
  * мутации заявок (``quantity``/``remaining``/``backing``), отмена опустевшего
    оффера и выставление строительной закупки остаются хуками движка: ядро
    задаёт только их порядок и аргументы;
  * отчёт, dirty goods, очередь и RNG не затрагиваются вовсе.

Смета считается здесь же, в ``construction_bill``: это чистая функция от
``cfg['construction']``, мощности и ``minimum_capacity``. Готовый словарь
передаётся ядру, поэтому у доставки и у резерва один и тот же источник чисел.

Эталонная Python-реализация живёт в этом же модуле: она нужна как образец для
focused-теста на побитовое совпадение и как fallback, если расширение не
собрано. Реализации принимают одни и те же аргументы, поэтому тест гоняет обе
на одинаковом состоянии и сравнивает словари, типы и порядок вызовов хуков.

Имена привязываются один раз при импорте, чтобы не платить за диспетчер на
каждом проекте. Модуль не импортирует ``modules.economy.support``: EPS
приходит аргументом, поэтому циклического импорта нет.
"""

import builtins
import importlib

from . import release_boundary

# Встроенные max/min сохраняют тип аргумента: ``max(0, x)`` оставляет целый 0
# целым, как в прежнем Python-коде, и int-остаток не превращается в float.
_PY_MAX = builtins.max
_PY_MIN = builtins.min

# Назначение строительной закупки. Строка повторяет прежний литерал: по ней
# ``project_cash_reserve`` и ``trade_kernel`` узнают материалы стройки.
MATERIAL_PURPOSE = 'Строительные материалы'
# Причина отмены опустевшего оффера: тот же текст, что и прежде. Константа
# приходит из границы release, чтобы текст причины не дублировался.
MATERIAL_RELEASE_REASON = release_boundary.RELEASE_REASON


def construction_bill_python(construction, minimum_capacity, capacity):
    """Эталон сметы: ``q*capacity/minimum_capacity`` в порядке словаря."""
    return {good: quantity*capacity/minimum_capacity
            for good, quantity in construction.items()}


def free_stock_hook(owner, account_id, good):
    """Хук доступного остатка: тело остаётся у движка (``V3.free_stock``)."""
    return owner.free_stock(account_id, good)


def detach_offer_hook(owner, store, offer, take):
    """Снять закреплённую часть собственного sell-оффера с рынка.

    Тело перенесено в ``release_boundary``/``release_kernel``: здесь остаётся
    только привязка имени, чтобы прежний call site резерва не менялся и
    второй записи полей оффера не появилось.
    """
    return release_boundary.detach_offer(owner, store, offer, take)


def settle_release_hook(owner, offer, good, eps):
    """Пометить котировки грязными и отменить опустевший оффер.

    Тело перенесено в ``release_boundary``/``release_kernel``: причина отмены и
    порядок ``dirty`` -> ``cancel`` остаются прежними.
    """
    return release_boundary.settle_release(owner, offer, good, eps)


def place_order_hook(owner, account, good, missing, sells):
    """Выставить строительную закупку на недостающий материал.

    Пустую заявку отбрасывает сам ``V3.order``: ядро зовёт хук на каждый
    материал, включая ``missing == 0``, как и прежний цикл.
    """
    _, price, _ = owner.quoted_cost(account, good, missing, sells)
    owner.order(account['id'], good, missing, 'buy', price, MATERIAL_PURPOSE)


def reserve_materials_python(owner, account, project, bill, sells, eps, store,
                             free_stock, detach_offer, settle_release, place_order):
    """Эталон прежнего участка ``project_material_requests``.

    Аргументы повторяют native-ядро один в один, чтобы focused-тест мог
    прогнать обе реализации на одинаковом состоянии и сравнить побитово.
    """
    materials = project['materials']
    stock = account['stock']
    reserved = account['stock_reserved']
    account_id = account['id']
    for good, quantity in bill.items():
        held = materials.get(good, 0)
        # Прежний порядок: сначала физический склад, затем свободный остаток.
        assign = _PY_MIN(_PY_MAX(0, quantity-held), _PY_MAX(0, stock.get(good, 0)-held))
        release = _PY_MAX(0, assign-free_stock(owner, account_id, good))
        for offer in sells.get(good, []):
            if offer['participant'] != account_id or offer['status'] in ('cancelled', 'expired'):
                continue
            take = _PY_MIN(release, offer['remaining'])
            if take <= eps:
                continue
            # С рынка снимается только закреплённая часть, свободный остаток
            # остаётся в продаже.
            detach_offer(owner, store, offer, take)
            reserved[good] = _PY_MAX(0, reserved.get(good, 0)-take)
            release = release-take
            settle_release(owner, offer, good, eps)
        # Пересчёт по свободному остатку после освобождения собственных офферов.
        assign = _PY_MIN(_PY_MAX(0, quantity-held), free_stock(owner, account_id, good))
        materials[good] = held+assign
        missing = _PY_MAX(0, quantity-materials[good])
        place_order(owner, account, good, missing, sells)
    return None


def load_material_kernel():
    """Вернуть native-модуль material_kernel или None, если он не собран.

    Отсутствие расширения даёт ImportError, а не ModuleNotFoundError: пакет
    ``modules.economy.native`` существует, а подмодуля нет. Обе ситуации
    означают одно — ядро не собрано, берём эталонную Python-ветку.
    """
    try:
        return importlib.import_module('.material_kernel', __package__)
    except (ImportError, ModuleNotFoundError):
        return None


NATIVE_MATERIAL_KERNEL = False

_kernel = load_material_kernel()
if _kernel is not None:
    construction_bill = _kernel.construction_bill
    reserve_materials = _kernel.reserve_materials
    NATIVE_MATERIAL_KERNEL = True
else:
    construction_bill = construction_bill_python
    reserve_materials = reserve_materials_python
