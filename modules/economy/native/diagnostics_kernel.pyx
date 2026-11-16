# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Diagnostics kernel: числовые counters компактного event trace.

Срез — агрегация сделок месяца для ``trade.executed`` событий
(``build_causal_events``): счётчики ``count`` и суммы ``quantity``, ``gold``,
``freight`` по ключу (buyer, seller, good, buyer_province, seller_province,
purpose). События при этом по-прежнему создаёт ``Economy.log_event``:
ядро лишь считает числа, порядок групп — порядок первого появления ключа в
отчёте сделок, поэтому порядок и количество событий не меняются.

Аккумуляторы стартуют с прежних типов (``count`` с int 0, суммы с float 0.0),
порядок прибавлений — порядок сделок в отчёте. Арифметика намеренно
``object``: количества и золото бывают и int, и float, и тип виден в
событиях. Числовая проверка инвариантов validate уже живёт в
settlement_kernel и сюда не входит.
"""


def aggregate_trade_groups(object trades):
    """Сгруппировать сделки месяца: counters по прежнему ключу и порядку.

    Возвращает список ``((buyer, seller, good, buyer_region, seller_region,
    purpose), count, quantity, gold, freight)`` в порядке первого появления
    ключа — ровно тот порядок, в каком прежний код обходил ``trade_groups``.
    """
    cdef object groups, order, trade, key, group
    groups = {}
    order = []
    for trade in trades:
        key = (trade.get('buyer'), trade.get('seller'), trade.get('good'),
               trade.get('buyer_province'), trade.get('seller_province'),
               trade.get('purpose'))
        group = groups.get(key)
        if group is None:
            group = groups[key] = [0, 0.0, 0.0, 0.0]
            order.append(key)
        group[0] += 1
        group[1] += trade.get('quantity', 0)
        group[2] += trade.get('gold', 0)
        group[3] += trade.get('freight', 0)
    return [(key, groups[key][0], groups[key][1], groups[key][2],
             groups[key][3]) for key in order]
