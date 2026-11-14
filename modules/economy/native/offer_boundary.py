"""Граница представления офферов: одна привязка реализации на импорт.

Модуль не хранит состояние: книга принадлежит движку (``engine._market_books``),
а представление лишь читает её живые объекты заявок. Поэтому копий orders нет
ни здесь, ни в extension.

Поля заявок принадлежат Cython-колонкам ``OrderStore``. Книга хранит номера
слотов рядом с тройками (``book['slots']`` и ``book['slot_store']``), поэтому
обход читает колонку по номеру и не ищет ``id(order)`` на каждом оффере. Заявка
без слота — это снимок planning-книги; для неё единственный источник значений
остаётся её словарь.

Эталонная Python-реализация живёт в этом же модуле и повторяет V3 буквально:
она нужна как образец для focused-теста и как fallback, если расширение не
собрано. Имена ``OfferView``/``iterator`` привязываются один раз при импорте,
чтобы не платить за диспетчер на каждом обходе.

Модуль не импортирует ``modules.economy.support``: EPS приходит через
``configure()``, иначе получается циклический импорт (support -> boundary ->
support).
"""

import importlib

# Допуск остатка. Переопределяется configure() из support.py.
_EPS = 1e-7


def configure(eps):
    """Задать EPS проекта для native-представления и Python-эталона."""
    global _EPS
    _EPS = eps
    if _view is not None:
        _view.set_epsilon(eps)


def _columns(book):
    """Колонки книги: (slots, remaining, status) или None для снимка.

    Номера считаются один раз — при построении книги или первом обходе. Сверка
    с ``version`` нужна только после сдвига раскладки (``remove``/пересборка) и
    после подмены хранилища; она стоит одно чтение поля на обход.
    """
    store = book.get('slot_store')
    if store is None:
        return None
    options = book['options']
    slots = book.get('slots')
    if (slots is None or len(slots) != len(options)
            or book.get('slot_version') != store.version):
        slots = store.slots([row[1] for row in options])
        book['slots'] = slots
        book['slot_version'] = store.version
    return slots, store.remaining, store.status


def _is_live(sell, slot, remaining, status):
    """Живость оффера: остаток выше допуска и статус не снят.

    При неотрицательном слоте значения читаются из колонок хранилища, иначе из
    словаря заявки: у снимка колонок нет.
    """
    if slot >= 0:
        left, state = remaining[slot], status[slot]
    else:
        left, state = sell['remaining'], sell['status']
    return left > _EPS and state not in ('cancelled', 'expired')


class OfferViewPython:
    """Эталон V3: ленивое представление книги без копирования офферов."""

    def __init__(self, book, buyer_id, province_id, local_only=False):
        self.book = book
        self.buyer_id = buyer_id
        self.province_id = province_id
        self.local_only = local_only

    def __iter__(self):
        book = self.book
        options = book['options']
        first = book['first_live']
        columns = _columns(book)
        slots, remaining, status = columns if columns is not None else (None, None, None)
        limit = len(options)
        if first > limit:
            first = limit
        while first < limit:
            sell = options[first][1]
            slot = slots[first] if slots is not None else -1
            if _is_live(sell, slot, remaining, status):
                break
            first += 1
        book['first_live'] = first
        for index in range(first, limit):
            price, sell, quote = options[index]
            slot = slots[index] if slots is not None else -1
            if (sell['participant'] != self.buyer_id
                    and _is_live(sell, slot, remaining, status)
                    and (not self.local_only
                         or sell['province_id'] == self.province_id)):
                yield price, sell, quote

    def __bool__(self):
        return next(iter(self), None) is not None

    def __getitem__(self, index):
        if index < 0:
            raise IndexError('OfferView не поддерживает отрицательный индекс')
        for current, row in enumerate(self):
            if current == index:
                return row
        raise IndexError(index)


def load_offer_view():
    """Вернуть native-модуль offer_view или None, если он не собран.

    Отсутствие расширения даёт ImportError, а не ModuleNotFoundError: пакет
    ``modules.economy.native`` существует, а подмодуля нет. Обе ситуации
    означают одно — расширение не собрано, берём эталон.
    """
    try:
        return importlib.import_module('.offer_view', __package__)
    except (ImportError, ModuleNotFoundError):
        return None


NATIVE_OFFER_VIEW = False

_view = load_offer_view()
if _view is not None:
    OfferView = _view.OfferView
    NATIVE_OFFER_VIEW = True
else:
    OfferView = OfferViewPython
