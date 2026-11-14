"""Граница представления офферов: одна привязка реализации на импорт.

Модуль не хранит состояние: книга принадлежит движку (``engine._market_books``),
а представление лишь читает её живые объекты заявок. Поэтому копий orders нет
ни здесь, ни в extension.

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


class OfferViewPython:
    """Эталон V3: ленивое представление книги без копирования офферов."""

    def __init__(self, book, buyer_id, province_id, local_only=False):
        self.book = book
        self.buyer_id = buyer_id
        self.province_id = province_id
        self.local_only = local_only

    def __iter__(self):
        options = self.book['options']
        first = self.book['first_live']
        while first < len(options):
            sell = options[first][1]
            if _is_live(sell):
                break
            first += 1
        self.book['first_live'] = first
        for index in range(first, len(options)):
            price, sell, quote = options[index]
            if (sell['participant'] != self.buyer_id
                    and _is_live(sell)
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


def _is_live(sell):
    """Живость оффера: остаток выше допуска и статус не снят."""
    return (sell['remaining'] > _EPS
            and sell['status'] not in ('cancelled', 'expired'))


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
