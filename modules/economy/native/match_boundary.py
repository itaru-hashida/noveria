"""Граница внутреннего цикла покупателя: одна привязка реализации на импорт.

Модуль не хранит состояние и не копирует заявки: ядро получает сам движок,
его ``OfferView`` (книгу с живыми объектами заявок), ``CashStore`` и
``OrderStore``. Внешний цикл покупателей, выбор market book, цены и количество
``q`` остаются в Python-вызове, а обход книги и вызовы ``commit_trade`` уходят
в скомпилированный цикл.

Эталонная Python-реализация живёт в этом же модуле: она нужна как образец для
focused-теста на побитовое совпадение и как fallback, если расширение не
собрано. Имена привязываются один раз при импорте, чтобы не платить за
диспетчер на каждом buyer order.

Модуль не импортирует ``modules.economy.support``: EPS приходит аргументом,
``shuffled``/``purchase_quantity`` — хуки движка, поэтому циклического импорта
нет.
"""

import builtins
import importlib
import math

# Встроенный min сохраняет тип аргумента: ``q`` остаётся целым, если все
# аргументы целые, — ровно как в прежнем Python-цикле.
_PY_MIN = builtins.min


def _row_id(row):
    """Ключ tie-break: ID заявки продавца (прежний ``lambda row: row[1]['id']``)."""
    return row[1]['id']


def _same_level(left, right):
    """Равная полная цена: то же условие, что ``math.isclose`` в V3."""
    return left == right or math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-12)


def run_match_python(owner, buy, options, sells, eps, cash, store, commit):
    """Эталон прежнего участка ``match`` после построения ``OfferView``.

    Аргументы повторяют native-ядро один в один, чтобы focused-тест мог
    прогнать обе реализации на одинаковом состоянии и сравнить побитово.
    """
    purchase = owner.purchase_quantity
    iterator = iter(options)
    pending = next(iterator, None)
    while pending is not None:
        price_level = pending[0]
        # Группа равной полной цены: только она перемешивается, чтобы равная
        # цена не давала постоянного преимущества одному ID.
        tied = [pending]
        pending = next(iterator, None)
        while pending is not None and _same_level(price_level, pending[0]):
            tied.append(pending)
            pending = next(iterator, None)
        for price, sell, quote in owner.shuffled(tied, key=_row_id):
            if price > buy['price']+eps:
                continue
            q = _PY_MIN(buy['remaining'], sell['remaining'], buy['reserved']/price,
                        purchase(buy, sells))
            if q <= eps:
                continue
            commit(owner, buy, sell, quote, q, price, eps, cash, store)
            if buy['remaining'] <= eps:
                break
        if buy['remaining'] <= eps:
            break


def load_match_kernel():
    """Вернуть native-модуль match_kernel или None, если он не собран.

    Отсутствие расширения даёт ImportError: пакет ``modules.economy.native``
    существует, а подмодуля нет. Это значит одно — ядро не собрано, берём
    эталон.
    """
    try:
        return importlib.import_module('.match_kernel', __package__)
    except (ImportError, ModuleNotFoundError):
        return None


NATIVE_MATCH_KERNEL = False

_kernel = load_match_kernel()
if _kernel is not None:
    run_match = _kernel.run_match
    NATIVE_MATCH_KERNEL = True
else:
    run_match = run_match_python
