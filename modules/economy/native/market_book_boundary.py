"""Граница рыночной книги: одна привязка ядра на импорт.

Модуль не хранит состояние. Книга по-прежнему принадлежит движку
(``engine._market_books``), кэш доставки — тоже (``engine._landed_cache``),
живые заявки не копируются. Здесь только выбор реализации: Cython-ядро
(``market_book_kernel``) либо буквальный Python-эталон из этого файла.

Эталон нужен по двум причинам: он остаётся образцом для focused-теста
(«Python reference == Cython») и работает как fallback, если расширение не
собрано. Имена ``landed``/``build_options`` привязываются один раз при импорте,
поэтому диспетчера на каждый вызов нет; эталон доступен как
``landed_python``/``build_options_python``.

Что остаётся в Python и приходит в ядро хуками:

  * ``market_tile`` и ``route_component`` — движок;
  * ``_orders_in_route_component`` и ``_market_source_groups`` — индексы
    достижимых заявок и потоков площадок;
  * ``OfferView``, ``_record_market_board``, diagnostics и invalidation —
    представление, отчёт и сброс кэшей двигает движок.

Ядро не читает ``remaining``/``status``: книга не замораживает заявки и не
копирует их. Сделки меняют те же объекты, представление перечитывает колонки.

Проверка свежести книги (``market_book_is_fresh``) — дешёвый guard от stale
cache: книга принадлежит конкретному списку офферов и конкретной эпохе полной
инвалидации котировок. Обычный ход эпоху не меняет, поэтому hit/miss-семантика
остаётся прежней; смена эпохи означает, что книгу пересобрали в обход
``invalidate_market_books`` (редактор мира, лабораторный сценарий).
"""

import heapq
import importlib


def landed_python(engine, seller_tile, buyer_tile, good, price):
    """Полная цена товара (эталон V3): кэш движка, маршрут и доставка."""
    cache = engine.__dict__.get('_landed_cache')
    if cache is None:
        cache = {}
    key = (seller_tile, buyer_tile, good)
    quote = cache.get(key)
    if quote is None:
        distance, path = engine.path(seller_tile, buyer_tile)
        if distance is None:
            cache[key] = False
            engine.__dict__['_landed_cache'] = cache
            return None
        weight, rate = engine.freight_factors(good)
        # Произведение считается слева направо: distance*(weight*rate) даёт
        # другой последний бит и разводит траекторию с первого месяца.
        quote = (distance*weight*rate, distance, path)
        cache[key] = quote
        engine.__dict__['_landed_cache'] = cache
    if quote is False:
        return None
    freight, distance, path = quote
    return price+freight, freight, distance, path


def option_key_python(item):
    """Ключ сортировки книги: ``(landed_price, sell_id)``."""
    return (item[0], item[1]['id'])


def stream_key_python(item):
    """Ключ слияния потоков площадок: та же пара ``(price, id)``."""
    return item[0]


def build_options_python(engine, sell_list, buyer_tile, good, landed_hook):
    """Собрать ``options`` книги (эталон V3, буквально).

    Хуки берутся у движка: индексы достижимости и потоки площадок живут там,
    и второй копии этих индексов ядро не заводит. ``landed_hook`` — привязанный
    метод движка, поэтому переопределение ``landed`` в наследнике сохраняется.
    """
    if len(sell_list) < 1000:
        options = []
        for sell in engine._orders_in_route_component(sell_list, buyer_tile):
            quote = landed_hook(sell['tile_id'], buyer_tile, good,
                                sell['price'])
            if quote:
                options.append((quote[0], sell, quote))
        options.sort(key=option_key_python)
        return options
    # Большая книга: цена внутри площадки уже отсортирована, доставка
    # добавляет площадке одну и ту же константу, поэтому потоки сливаются
    # кучей, а не сортируются заново.
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
            for _, price, sell, quote in heapq.merge(*streams,
                                                     key=stream_key_python)]


def build_options_batch(engine, sell_list, buyer_tile, good, landed_hook,
                        *, additive_freight=False):
    """Построить малую книгу, вызывая landed один раз на площадку."""
    if not additive_freight or len(sell_list) >= 1000 or _kernel is None or not hasattr(
            _kernel, 'build_options_from_freight'):
        return build_options(engine, sell_list, buyer_tile, good, landed_hook)
    reachable = engine._orders_in_route_component(sell_list, buyer_tile)
    freight_by_tile = {}
    for sell in reachable:
        tile = sell['tile_id']
        if tile not in freight_by_tile:
            freight_by_tile[tile] = landed_hook(tile, buyer_tile, good, 0)
    return _kernel.build_options_from_freight(reachable, freight_by_tile)


def market_book_is_fresh_python(entry, sell_list, engine):
    """Книга принадлежит этому набору офферов и этой эпохе котировок."""
    if entry is None:
        return False
    if entry.get('source') is not sell_list:
        return False
    return entry.get('quote_epoch', 0) == engine.__dict__.get(
        '_market_quotes_epoch', 0)


def market_book_is_fresh_epoch(entry, sell_list, epoch):
    """Чистая проверка книги по явно подготовленной эпохе."""
    if entry is None or entry.get('source') is not sell_list:
        return False
    return entry.get('quote_epoch', 0) == epoch


def load_market_book():
    """Вернуть native-модуль market_book_kernel или None, если он не собран.

    Отсутствие расширения даёт ImportError, а не ModuleNotFoundError: пакет
    ``modules.economy.native`` существует, а подмодуля нет. Обе ситуации
    означают одно — расширение не собрано, берём эталон.
    """
    try:
        return importlib.import_module('.market_book_kernel', __package__)
    except (ImportError, ModuleNotFoundError):
        return None


NATIVE_MARKET_BOOK = False

_kernel = load_market_book()
if _kernel is not None:
    # Engine-facing operations remain in this boundary. Native receives only
    # prepared freight values through build_options_batch.
    landed = landed_python
    build_options = build_options_python
    market_book_is_fresh = market_book_is_fresh_epoch
    option_key = _kernel.option_key
    stream_key = stream_key_python
    NATIVE_MARKET_BOOK = True
else:
    landed = landed_python
    build_options = build_options_python
    market_book_is_fresh = market_book_is_fresh_epoch
    option_key = option_key_python
    stream_key = stream_key_python
