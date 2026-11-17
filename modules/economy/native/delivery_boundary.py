"""Граница доставки строительных материалов: одна привязка реализации на импорт.

Модуль не хранит состояние движка и не создаёт второго склада: ``account`` и
``project`` — живые словари движка, поэтому ``project['delivery']`` и
``account['protected']``, которые меняет ядро, — те же поля, что читает
остальной движок. ``protected`` при этом остаётся денежной колонкой
``CashStore``: политика считается здесь, а пишет значение владелец полей.
Материалы в проект, склад и резерв применяет существующая
граница резерва (``material_boundary``), а не вторая копия её логики.

Что перенесено в ядро (``delivery_kernel``): объём перевозки
(``sum(q*weight)`` по смете), её стоимость (``volume*distance*transport_rate``)
и запись delivery-зависимого числового состояния ``project['delivery']``.
Добавление доставленного материала в ``project['materials']`` и уменьшение
склада/резерва уже живут в ``material_kernel``: эта граница вызывает его, а не
повторяет, поэтому второй записи материала и второго склада нет.

Что осталось в Python и почему:

  * выбор проекта, порядок инвесторов, отмена по простою и по марже/спросу,
    проверка ``distance is None`` — целиком в ``v3.project_material_requests``
    до этого вызова;
  * ``protected cash`` — хук ``set_project_protection``: денежная политика
    (слагаемые, урезание по ``balance-reserved``) остаётся у движка, ядро
    задаёт только момент вызова и аргументы; значение пишет ``CashStore``;
  * материалы, склад, резерв, ``stock_reserved``, строительные закупки,
    ``cancel_order`` и ``mark_market_quotes_dirty`` — существующая граница
    ``material_boundary``: доставка лишь считает числа и защищает деньги
    **до** резерва, иначе ``free_cash`` не учёл бы доставку;
  * ``free_cash``, ``order``, отчёты, события, dirty goods и очередь не
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

from . import cash_boundary
from . import material_boundary

# Встроенный max сохраняет тип аргумента: ``max(0, x)`` оставляет целый 0
# целым, как в прежнем Python-коде.
_PY_MAX = builtins.max
# Встроенный sum: float-слагаемые CPython 3.12 складывает компенсированно,
# поэтому объём обязан считаться той же функцией, что и прежде.
_PY_SUM = builtins.sum


def delivery_volume_python(bill, goods):
    """Эталон объёма: ``sum(q*weight)`` в порядке сметы."""
    return _PY_SUM([quantity*goods[good]['weight']
                    for good, quantity in bill.items()])


def delivery_cost_python(bill, goods, distance, transport_rate):
    """Эталон стоимости: ``volume*distance*transport_rate`` прежним порядком."""
    return delivery_volume_python(bill, goods)*distance*transport_rate


def set_project_protection(owner, account, project, delivery, eps):
    """Хук защиты денег: те же слагаемые и то же урезание, что и прежде.

    Сначала склад инвестора, затем площадка: обе перевозки реальны и входят в
    оценку. Денежная политика (слагаемые и урезание по ``balance-reserved``)
    остаётся здесь, а сами значения полей пишет ``CashStore``: ``protected`` и
    ``reserved`` — его колонки, и второй писатель завёл бы второе состояние.
    """
    cash = cash_boundary.store_for(owner, account)
    hold = (account.get('operating_protected', 0)
            +account.get('payroll_protected', 0)
            +project['working_cash']+delivery)
    cash.set_protected_cash(account['id'], hold)
    if (cash.get_protected_cash(account['id'])
            +cash.get_reserved_cash(account['id'])
            > cash.get_balance(account['id'])+eps):
        cash.set_protected_cash(account['id'],
            _PY_MAX(0, cash.get_balance(account['id'])
                    -cash.get_reserved_cash(account['id'])))


def plan_delivery_python(owner, account, project, bill, distance, eps,
                         set_protection):
    """Эталон прежнего участка: объём, стоимость, запись и защита денег."""
    delivery = delivery_cost_python(bill, owner.goods, distance,
                                    owner.cfg['transport_rate'])
    project['delivery'] = delivery
    set_protection(owner, account, project, delivery, eps)
    return delivery


def request_project_materials_python(owner, account, sells, eps, store, distance):
    """Эталон порядка: смета -> доставка и ``protected`` -> резерв материалов."""
    project = account['project']
    bill = material_boundary.construction_bill(
        owner.cfg['construction'], owner.cfg['minimum_capacity'],
        project['capacity'])
    plan_delivery_python(owner, account, project, bill, distance, eps,
                         set_project_protection)
    material_boundary.reserve_materials(
        owner, account, project, bill, sells, eps, store,
        material_boundary.free_stock_hook,
        material_boundary.detach_offer_hook,
        material_boundary.settle_release_hook,
        material_boundary.place_order_hook)
    return bill


def request_project_materials(owner, account, sells, eps, store, distance):
    """Смета, доставка, защита денег и резерв материалов выбранного проекта.

    Порядок прежний и обязательный: смета -> доставка и ``protected`` ->
    резерв материалов -> строительные закупки. ``protected`` выставлен до
    резерва, иначе ``free_cash`` не учёл бы доставку и оборотный капитал.
    """
    project = account['project']
    bill = material_boundary.construction_bill(
        owner.cfg['construction'], owner.cfg['minimum_capacity'],
        project['capacity'])
    plan_delivery(owner, account, project, bill, distance, eps,
                  set_project_protection)
    material_boundary.reserve_materials(
        owner, account, project, bill, sells, eps, store,
        material_boundary.free_stock_hook,
        material_boundary.detach_offer_hook,
        material_boundary.settle_release_hook,
        material_boundary.place_order_hook)
    return bill


def load_delivery_kernel():
    """Вернуть native-модуль delivery_kernel или None, если он не собран.

    Отсутствие расширения даёт ImportError, а не ModuleNotFoundError: пакет
    ``modules.economy.native`` существует, а подмодуля нет. Обе ситуации
    означают одно — ядро не собрано, берём эталонную Python-ветку.
    """
    try:
        return importlib.import_module('.delivery_kernel', __package__)
    except (ImportError, ModuleNotFoundError):
        return None


NATIVE_DELIVERY_KERNEL = False

_kernel = load_delivery_kernel()
if _kernel is not None:
    delivery_volume = _kernel.delivery_volume
    delivery_cost = _kernel.delivery_cost
    plan_delivery = _kernel.plan_delivery
    NATIVE_DELIVERY_KERNEL = True
else:
    delivery_volume = delivery_volume_python
    delivery_cost = delivery_cost_python
    plan_delivery = plan_delivery_python
