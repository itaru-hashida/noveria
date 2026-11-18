"""Подготовка и применение резервов стройки по живым остаткам и офферам."""

import builtins
import importlib
from .stock_store_boundary import get_stock_store

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


def assignable_stock_python(needed, stock, held):
    return _PY_MIN(needed, _PY_MAX(0, stock-held))


def assign_from_free_stock_python(needed, free, held):
    return held+_PY_MIN(needed, free)


def _reserve_materials(owner, account, project, bill, sells, eps, store,
                       free_stock, detach_offer, settle_release, place_order,
                       assignable, assign_final):
    """Зарезервировать материалы и разместить заявки в порядке сметы."""
    materials = project['materials']
    stock = account['stock']
    stock_store = get_stock_store(owner)
    account_id = account['id']
    for good, quantity in bill.items():
        held = materials.get(good, 0)
        # Прежний порядок: сначала физический склад, затем свободный остаток.
        assign = assignable(_PY_MAX(0, quantity-held), stock.get(good, 0), held)
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
            stock_store.release_reserved(account_id, good, take)
            release = release-take
            settle_release(owner, offer, good, eps)
        # Пересчёт по свободному остатку после освобождения собственных офферов.
        materials[good] = assign_final(
            _PY_MAX(0, quantity-held), free_stock(owner, account_id, good), held)
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


def reserve_materials_python(owner, account, project, bill, sells, eps, store,
                             free_stock, detach_offer, settle_release, place_order):
    return _reserve_materials(owner, account, project, bill, sells, eps, store,
                              free_stock, detach_offer, settle_release, place_order,
                              assignable_stock_python, assign_from_free_stock_python)


def reserve_materials(owner, account, project, bill, sells, eps, store,
                      free_stock, detach_offer, settle_release, place_order):
    return _reserve_materials(owner, account, project, bill, sells, eps, store,
                              free_stock, detach_offer, settle_release, place_order,
                              assignable_stock, assign_from_free_stock)


_kernel = load_material_kernel()
construction_bill = getattr(_kernel, 'construction_bill', construction_bill_python)
assignable_stock = getattr(_kernel, 'assignable_stock', assignable_stock_python)
assign_from_free_stock = getattr(
    _kernel, 'assign_from_free_stock', assign_from_free_stock_python)
NATIVE_MATERIAL_KERNEL = assignable_stock is not assignable_stock_python
