# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Внутренний цикл исполнения одного buyer order: обход книги и вызов сделки.

Python по-прежнему формирует порядок buyer orders и market book: сюда приходит
один buyer order и уже построенное ``OfferView``. Ядро повторяет участок
``Economy.match`` буквально:

  * обход идёт через ``iter(options)``, поэтому порядок offers, курсор
    ``first_live``, фильтр ``local_only`` и проверка живости остаются делом
    представления, а не копией книги;
  * offers группируются по равной полной цене тем же условием, что
    ``math.isclose`` с ``rel_tol=abs_tol=1e-12`` (точное равенство даёт тот же
    ответ, а вызов Python-функции нужен только на смене ценового уровня);
  * внутри группы порядок задаёт ``owner.shuffled(tied, key=...)``: прежний
    tie-break по ID заявки и прежний расход RNG;
  * ``q`` считается тем же ``min`` и той же формулой, включая хук движка
    ``purchase_quantity``;
  * сделка исполняется существующим ``commit_trade``, полученным аргументом.

Копий заявок нет: ``sell`` и ``quote`` — живые объекты книги, а ``remaining``
и ``reserved`` читаются из тех же словарей заявок, которые меняет граница.
Тела ``transfer``/``use_stock``/``add_stock`` остаются у движка, поэтому их
переопределение в наследниках, сценариях и тестах продолжает работать.

Новых транзакционных правил здесь нет: цикл останавливается только по
``buy['remaining'] <= eps``, как и раньше. Rollback не добавляется.
"""

import builtins
import math

# Встроенный min сохраняет тип аргумента: ``q`` остаётся целым, если все
# аргументы целые, — ровно как в прежнем Python-цикле.
_PY_MIN = builtins.min


def _row_id(object row):
    """Ключ tie-break: ID заявки продавца (прежний ``lambda row: row[1]['id']``)."""
    return row[1]['id']


cdef inline bint _same_level(object left, object right) except *:
    """Равная полная цена: то же условие, что ``math.isclose`` в V3.

    Быстрый путь — точное равенство (первый шаг ``math.isclose``); вызов
    Python-функции остаётся только на смене ценового уровня, поэтому на каждом
    оффере цикла за него платить не нужно.
    """
    if left == right:
        return True
    return math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-12)


cdef inline object _next(object iterator):
    """Следующий оффер или None: ``next(ordered, None)`` из прежнего цикла."""
    try:
        return next(iterator)
    except StopIteration:
        return None


def run_match(object owner, object buy, object options, object sells, object eps,
              object cash, object store, object commit):
    """Пройти живые офферы одного buyer order до полного исполнения.

    ``owner`` нужен только ради хуков ``purchase_quantity`` и ``shuffled``;
    они берутся один раз на заявку, потому что внутри цикла мутаций много.
    """
    cdef object iterator = iter(options)
    cdef object purchase = owner.purchase_quantity
    cdef object shuffle = owner.shuffled
    cdef object pending = _next(iterator)
    cdef object price_level
    cdef object tied
    cdef object row
    cdef object price
    cdef object sell
    cdef object quote
    cdef object q

    while pending is not None:
        price_level = pending[0]
        # Группа равной полной цены: только она перемешивается, чтобы равная
        # цена не давала постоянного преимущества одному ID. В памяти живёт
        # одна текущая группа, а не копия всей книги.
        tied = [pending]
        pending = _next(iterator)
        while pending is not None and _same_level(price_level, pending[0]):
            tied.append(pending)
            pending = _next(iterator)
        for row in shuffle(tied, key=_row_id):
            price = row[0]
            if price > buy['price']+eps:
                continue
            sell = row[1]
            quote = row[2]
            q = _PY_MIN(buy['remaining'], sell['remaining'],
                        buy['reserved']/price, purchase(buy, sells))
            if q <= eps:
                continue
            commit(owner, buy, sell, quote, q, price, eps, cash, store)
            if buy['remaining'] <= eps:
                break
        if buy['remaining'] <= eps:
            break
    return None
