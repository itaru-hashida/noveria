# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Резерв строительных материалов: смета, склад инвестора и stock_reserved.

Ядро — только вертикальный срез ``project_material_requests``: расчёт сметы,
проверка доступного склада, резерв материалов и обновление ``stock_reserved``.
Всё, что вокруг, остаётся у Python:

  * проект выбирает ``v3.project_material_requests``: сюда приходит уже
    выбранный проект и его готовая смета, порядок проектов задаёт
    перемешанный список инвесторов;
  * отмена по простою и по марже/спросу, доставка и защита денег считаются в
    Python вокруг вызова ядра;
  * заявки и склад не копируются: ``account`` — живой словарь счёта, поэтому
    ``stock_reserved`` и ``project['materials']``, которые меняет ядро, — те же
    вложенные словари, что видит остальной движок. Второго склада ядро не
    создаёт;
  * мутации собственного sell-оффера (``quantity``/``remaining``/``backing``),
    пометка котировок грязными, отмена опустевшего оффера и выставление
    строительной закупки остаются Python-хуками: ядро вызывает их в прежнем
    порядке и только между ними меняет ``stock_reserved``;
  * rollback не добавляется: частично закреплённые материалы остаются
    закреплёнными, как и раньше.

Арифметика намеренно ``object``, а не ``double``. Смета — ``q*capacity/
minimum_capacity``: при целых входах деление даёт float, но промежуточное
произведение обязано остаться точным. Остатки склада бывают и ``int``, и
``float``, а ``min``/``max`` с целым нулём сохраняют тип аргумента, и этот тип
виден дальше в отчёте. Поэтому используются встроенные ``min``/``max`` над
``object``: они порождают те же PyNumber_*, что и интерпретатор.

Порядок операций сохранён буквально, в том числе:

  * ``min(max(0, quantity-held), max(0, stock-held))`` — встроенный ``min``
    при равенстве возвращает первый аргумент;
  * ``assign`` пересчитывается по ``free_stock`` только после освобождения
    собственных офферов, а не переиспользует прежнее значение;
  * ``take = min(release, offer['remaining'])`` и пропуск ``take <= EPS``
    выполняются до любой мутации оффера;
  * ``stock_reserved`` уменьшается ровно между мутацией полей оффера и
    пометкой котировок грязными — как в прежнем теле;
  * ``held`` читается один раз на материал, а ``missing`` считается уже от
    записанного ``project['materials'][good]``;
  * строительная закупка выставляется на каждый материал, включая ``missing``
    равный нулю: ``order`` сам отбрасывает пустую заявку.
"""

import builtins

# Встроенные max/min: на границе ``max(0, x)`` они сохраняют тип аргумента,
# поэтому целый 0 остаётся целым, как в прежнем Python-коде.
_PY_MAX = builtins.max
_PY_MIN = builtins.min


def construction_bill(object construction, object minimum_capacity, object capacity):
    """Смета проекта: та же формула ``q*capacity/minimum_capacity``.

    Порядок обхода — порядок ``cfg['construction']``, поэтому ключи сметы
    идут в том же порядке, что и у прежнего comprehension. Значения остаются
    ``object``: произведение точное, а деление даёт float ровно там же, где
    давало его прежнее выражение.
    """
    cdef dict bill = {}
    cdef object good
    cdef object quantity
    for good, quantity in construction.items():
        bill[good] = quantity*capacity/minimum_capacity
    return bill


def reserve_materials(object owner, object account, object project, object bill,
                      object sells, object eps, object store, object free_stock,
                      object detach_offer, object settle_release, object place_order):
    """Закрепить материалы проекта и выставить недостающие закупки.

    ``bill`` — уже посчитанная смета проекта, ``free_stock`` — хук движка
    ``free_stock(owner, account_id, good)``: доступный остаток с учётом
    ``stock_reserved`` и материалов других проектов. ``detach_offer`` снимает
    закреплённую часть собственного sell-оффера, ``settle_release`` помечает
    котировки и отменяет опустевший оффер, ``place_order`` выставляет
    строительную закупку. Тела хуков остаются в Python, ядро задаёт только их
    порядок и аргументы.
    """
    cdef dict materials = project['materials']
    cdef dict stock = account['stock']
    cdef dict reserved = account['stock_reserved']
    cdef object account_id = account['id']
    cdef object good
    cdef object quantity
    cdef object held
    cdef object assign
    cdef object release
    cdef object missing
    cdef object offers
    cdef object offer
    cdef object take
    cdef Py_ssize_t index, count
    for good, quantity in bill.items():
        held = materials.get(good, 0)
        # Прежний порядок: сначала физический склад, затем свободный остаток.
        assign = _PY_MIN(_PY_MAX(0, quantity-held), _PY_MAX(0, stock.get(good, 0)-held))
        release = _PY_MAX(0, assign-free_stock(owner, account_id, good))
        offers = sells.get(good, [])
        count = len(offers)
        # Обход по индексу, а не по итератору списка: тот же живой список
        # ``sells[good]``, но без создания итератора на материал.
        for index in range(count):
            offer = <object>offers[index]
            if offer['participant'] != account_id or offer['status'] in ('cancelled', 'expired'):
                continue
            take = _PY_MIN(release, offer['remaining'])
            if take <= eps:
                continue
            # С рынка снимается только закреплённая часть, свободный остаток
            # остаётся в продаже: поля оффера меняет Python-хук.
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
