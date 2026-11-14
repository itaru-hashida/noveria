# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Доставка строительных материалов: объём партии, тариф и numeric state.

Ядро — только вертикальный срез доставки внутри
``project_material_requests``. Пять пунктов среза распределены так:

  1. расчёт объёма перевозки — ``delivery_volume`` здесь;
  2. расчёт стоимости доставки — ``delivery_cost`` здесь;
  3. добавление доставленного материала в ``project['materials']`` — уже
     перенесено раньше в ``material_kernel.reserve_materials``: ядро доставки
     не заводит вторую запись материала, а передаёт готовую смету этой
     существующей границе;
  4. уменьшение склада и резерва — там же: ``material_kernel`` меняет живой
     ``account['stock_reserved']`` и снимает закреплённую часть собственного
     sell-оффера через ``store``;
  5. обновление delivery-зависимого числового состояния — ``plan_delivery``
     здесь: ``project['delivery']`` пишется ядром, ``account['protected']``
     выставляет переданный хук.

Границы среза:

  * проект выбирает и перемешивает ``v3.project_material_requests``: сюда
    приходит уже выбранный проект, готовая смета и расстояние;
  * отмена по простою и по марже/спросу, ``distance is None`` и порядок
    инвесторов не затрагиваются вовсе;
  * ``free_cash``/``protected cash``, ``order``/``cancel_order``,
    ``mark_market_quotes_dirty``, dirty goods, отчёты, события и очередь
    остаются Python-хуками: ядро задаёт только их порядок и аргументы;
  * rollback не добавляется.

Арифметика намеренно ``object``. Объём перевозки — ``sum(q*weight)`` по смете
в её порядке. CPython 3.12 складывает float-слагаемые компенсированным
алгоритмом (Neumaier), поэтому наивное накопление здесь недопустимо: оно
разошлось бы в последнем бите, а оттуда уехало бы в ``protected``,
``free_cash``, строительные закупки и весь отчёт. Список слагаемых строится
ядром в порядке сметы, а суммирует его тот же встроенный ``sum``: список и
прежний генератор дают для него побитово один и тот же результат.
Стоимость — ``volume*distance*transport_rate`` прежним порядком умножений.
"""

import builtins

# Встроенный sum: та же функция, что стояла в прежнем выражении, поэтому
# компенсация float-слагаемых и тип результата совпадают гарантированно.
_PY_SUM = builtins.sum


def delivery_volume(object bill, object goods):
    """Объём перевозки: ``sum(q*weight)`` в порядке сметы.

    ``goods`` — живой словарь товаров движка: вес берётся из него, копий нет.
    Смета обходится в её собственном порядке, поэтому список слагаемых идёт
    ровно в том же порядке, что и прежний генератор.
    """
    cdef list rows = []
    cdef object good
    cdef object quantity
    for good, quantity in bill.items():
        rows.append(quantity*goods[good]['weight'])
    return _PY_SUM(rows)


def delivery_cost(object bill, object goods, object distance, object transport_rate):
    """Стоимость доставки: ``volume*distance*transport_rate`` прежним порядком."""
    return delivery_volume(bill, goods)*distance*transport_rate


def plan_delivery(object owner, object account, object project, object bill,
                  object distance, object eps, object set_protection):
    """Посчитать доставку и записать delivery-зависимое состояние проекта.

    ``project['delivery']`` записывается до вызова ``set_protection``, потому
    что прежний код читал уже записанное поле. ``set_protection`` — хук
    движка: он складывает ``protected`` и урезает его по балансу, поэтому
    денежная политика остаётся в Python.
    """
    cdef object delivery = delivery_cost(bill, owner.goods, distance,
                                         owner.cfg['transport_rate'])
    project['delivery'] = delivery
    set_protection(owner, account, project, delivery, eps)
    return delivery
