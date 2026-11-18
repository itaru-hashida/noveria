# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Чистое ядро сборки рыночной книги.

Маршруты, кэш доставки, заявки и инвалидация принадлежат Python boundary.
Этот модуль получает только подготовленные заявки и числовые freight-данные.
Он не принимает ``engine`` и не вызывает market helpers.
"""


def option_key(object item):
    """Ключ сортировки книги: ``(landed_price, sell_id)``."""
    return (item[0], item[1]['id'])


def build_options_from_freight(object sells, dict freight_by_tile):
    """Собрать options из готовых числовых freight по площадкам."""
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
