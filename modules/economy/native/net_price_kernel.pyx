# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Net price kernel: числовой цикл ``V3.net_price``.

Перенесён ровно один участок — обход ``self.customer_quotes(e)`` внутри
``net_price``. Раньше этот цикл шёл в Python:

    consumer = e['good'] in (cfg['basic_needs'] | cfg['extra_needs'])
    quantity, final_quantity = 0.0, 0.0
    for order, limit in self.customer_quotes(e):
        if limit+EPS < e['price']:
            continue
        landed = e['price']+(order['price']-limit)
        volume = min(order['remaining'], order['reserved']/max(landed, EPS))
        quantity += volume
        if order.get('final_consumption'):
            final_quantity += volume
    share = (final_quantity/quantity if quantity > EPS else float(consumer))
    return e['price']*(1-self.cfg['consumption_tax']*share)

Что здесь принадлежит ядру: сам обход списка пар (order, limit), расчёт
``landed``/``volume``, накопление ``quantity`` и ``final_quantity``, затем
``share`` и итоговая цена. Список ``customer_quotes`` готовит Python: он
читает книгу заявок движка и не мутируется во время вызова, поэтому вход
стабилен. Порядок пар сохраняется буквально, RNG ядро не трогает.

Ядро сознательно работает с ``object``-арифметикой, а не с ``double``.
Цены, остатки и резервы заявок бывают и int, и float, а тип результата виден
дальше: в ``project_candidate`` он попадает в ``margin``, а затем в
``expected_profit`` проекта. Арифметика над ``object`` в Cython порождает те
же PyNumber_*, что и интерпретатор, поэтому значения и типы совпадают
побитово, включая int + int -> int.

Порядок операций сохранён буквально:
  * ``limit+EPS < e['price']`` — сравнение в прежнем порядке, поэтому
    short-circuit и граница допуска те же;
  * ``landed`` считается до ``volume`` и один раз на заявку;
  * ``max(landed, EPS)`` — встроенный ``max``, при равенстве он возвращает
    ``landed`` и сохраняет его тип;
  * ``order['reserved']/max(...)`` — сначала деление, затем ``min`` с
    ``order['remaining']``; при равенстве ``min`` возвращает ``remaining``;
  * ``float(consumer)`` вызывается только в ветви ``quantity <= EPS``, как и
    раньше: при непустом наборе заявок он не выполняется вовсе.

``self.customer_quotes(e)`` остаётся Python-хуком границы: ядро получает уже
материализованный список и не читает книгу заявок само.
"""


cdef inline object _max(object left, object right):
    """Встроенный max: результат — left, если right не больше left.

    Направление сравнения как в CPython (``if right > left``): при равенстве
    и при NaN побеждает первый аргумент, поэтому тип результата сохраняется.
    """
    return right if right > left else left


cdef inline object _min(object left, object right):
    """Встроенный min: результат — left, если right не меньше left.

    Направление сравнения как в CPython (``if right < left``): при равенстве
    и при NaN побеждает первый аргумент, поэтому тип результата сохраняется.
    """
    return right if right < left else left


def net_price(object e, object quotes, object eps, object consumption_tax,
              bint consumer, object price_base):
    """Пройти котировки покупателей и вернуть net-цену.

    ``price_base`` — прежний ``e['price']``: он читается до цикла и больше не
    перечитывается, поэтому последующая смена поля не влияет на результат.
    """
    cdef object quantity = 0.0
    cdef object final_quantity = 0.0
    cdef object order, limit, landed, volume, share
    cdef Py_ssize_t index, count

    count = len(quotes)
    for index in range(count):
        order, limit = quotes[index]
        if limit+eps < price_base:
            continue
        landed = price_base+(order['price']-limit)
        volume = _min(order['remaining'], order['reserved']/_max(landed, eps))
        quantity += volume
        if order.get('final_consumption'):
            final_quantity += volume
    if quantity > eps:
        share = final_quantity/quantity
    else:
        share = float(consumer)
    return price_base*(1-consumption_tax*share)
