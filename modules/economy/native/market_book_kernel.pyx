# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Ядро рыночной книги: достижимые офферы, landed price и точная сортировка.

Ядро не владеет состоянием. Заявки остаются живыми объектами движка, кэш
доставки — его единственным кэшем (``engine._landed_cache``), а маршруты,
площадки и сам кэш книги приходят из Python-хуков. Здесь только числа и
порядок:

  * фильтр достижимых офферов выполняет движок
    (``_orders_in_route_component`` / ``_market_source_groups``), ядро получает
    уже готовый список или готовые потоки площадок;
  * ``landed`` читает и пишет тот же кэш движка, поэтому второго кэша нет и
    hit/miss-семантика остаётся прежней;
  * сортировка идёт ровно по ключу ``(landed_price, sell_id)``;
  * ``remaining`` и ``status`` не читаются: книга не замораживает заявки.

Обе ветви повторяют V3 буквально, включая большую ветвь с ``heapq.merge`` для
книг от 1000 офферов. Арифметика намеренно остаётся объектной: выражение было
``distance*weight*rate`` и считается слева направо, а приведение к ``double``
могло бы изменить тип результата у целочисленных расстояний и весов.
"""

import heapq


def landed(object engine, object seller_tile, object buyer_tile, object good,
           object price):
    """Полная цена товара по неизменной в течение месяца сети.

    Повторяет V3: ключ кэша — тройка (площадка продавца, площадка покупателя,
    товар); вес и тариф в ключ не входят, потому что это константы контента.
    Недостижимая пара запоминается как ``False``, поэтому повторный запрос не
    считает маршрут заново.
    """
    cdef object cache = engine.__dict__.get('_landed_cache')
    cdef object key
    cdef object quote
    cdef object distance
    cdef object path
    cdef object weight
    cdef object rate
    if cache is None:
        # Кэш создаётся только тем вызовом, который действительно что-то
        # посчитал: пустой атрибут движку не навязывается.
        cache = {}
    key = (seller_tile, buyer_tile, good)
    quote = cache.get(key)
    if quote is None:
        distance, path = engine.path(seller_tile, buyer_tile)
        if distance is None:
            cache[key] = False
            engine.__dict__['_landed_cache'] = cache
            return None
        # Пара «вес, тариф» берётся хуком движка: он же кэширует её в
        # ``_freight_factors``, и второй копии этих констант не появляется.
        weight, rate = engine.freight_factors(good)
        quote = (distance*weight*rate, distance, path)
        cache[key] = quote
        engine.__dict__['_landed_cache'] = cache
    if quote is False:
        return None
    freight, distance, path = quote
    return price+freight, freight, distance, path


def option_key(object item):
    """Ключ сортировки книги: ``(landed_price, sell_id)``."""
    return (item[0], item[1]['id'])


def build_options_from_freight(object sells, dict freight_by_tile):
    """Собрать малую книгу по одному freight на площадку продавца."""
    cdef list options = []
    cdef object sell
    cdef object freight
    cdef object quote
    for sell in sells:
        freight = freight_by_tile.get(sell['tile_id'])
        if freight is None:
            continue
        quote = (sell['price']+freight[1], freight[1], freight[2], freight[3])
        options.append((quote[0], sell, quote))
    options.sort(key=option_key)
    return options


def stream_key(object item):
    """Ключ слияния потоков площадок: та же пара ``(price, id)``."""
    return item[0]


def market_book_is_fresh(object entry, object sell_list, object engine):
    """Книга принадлежит этому набору офферов и этой эпохе котировок.

    Дешёвый guard от stale cache: эпоха поднимается только полной
    инвалидацией, поэтому обычный ход hit/miss не меняет.
    """
    cdef object epoch
    cdef object current
    if entry is None:
        return False
    if entry.get('source') is not sell_list:
        return False
    epoch = entry.get('quote_epoch', 0)
    current = engine.__dict__.get('_market_quotes_epoch', 0)
    return epoch == current


def build_options(object engine, object sell_list, object buyer_tile,
                  object good, object landed_hook):
    """Собрать ``options`` книги: ``[(landed_price, sell, quote), ...]``.

    Хуки берутся у движка: ``_orders_in_route_component(sell_list, tile)``
    отдаёт живые заявки достижимой компоненты маршрутов, а
    ``_market_source_groups`` — отсортированные потоки площадок.
    ``landed_hook`` — привязанный метод движка: расчёт landed живёт в этом
    ядре, но вызов идёт через движок, поэтому наследник, переопределивший
    ``landed``, продолжает работать. Порядок элементов и tie-break задаёт
    только это ядро.
    """
    cdef list options
    cdef list streams
    cdef object sell
    cdef object quote
    cdef object source_tile
    cdef object source_sells
    cdef object stream
    cdef object freight
    if len(sell_list) < 1000:
        options = []
        for sell in engine._orders_in_route_component(sell_list, buyer_tile):
            quote = landed_hook(sell['tile_id'], buyer_tile, good,
                                sell['price'])
            if quote:
                options.append((quote[0], sell, quote))
        options.sort(key=option_key)
        return options
    # Большая книга: цена внутри площадки уже отсортирована по цене продавца и
    # ID, доставка добавляет площадке одну и ту же константу, поэтому потоки
    # сливаются кучей, а не сортируются заново.
    streams = []
    for source_tile, source_sells in engine._market_source_groups(
            sell_list, buyer_tile):
        freight = landed_hook(source_tile, buyer_tile, good, 0)
        if freight is None:
            continue
        stream = []
        for sell in source_sells:
            quote = (sell['price']+freight[1], freight[1], freight[2],
                     freight[3])
            stream.append(((quote[0], sell['id']), quote[0], sell, quote))
        streams.append(stream)
    return [(price, sell, quote)
            for _, price, sell, quote in heapq.merge(*streams, key=stream_key)]
