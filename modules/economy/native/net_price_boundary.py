"""Граница net price: один вызов из ``V3.net_price``, эталон и fallback.

В Cython уходит только числовой цикл по ``customer_quotes``: сравнение
``limit+EPS`` с ценой, расчёт ``landed``/``volume``, накопление ``quantity`` и
``final_quantity``, затем ``share`` и итоговая цена.

Что остаётся Python-хуком: сам ``customer_quotes(e)``. Он читает книгу заявок
движка, ничего не мутирует и возвращает готовый список пар
``(order, limit)``; ядро обходит его в том же порядке, что и прежний цикл.
RNG граница не трогает.

Эталонная Python-ветка (``net_price_python``) повторяет прежнее тело
``Economy.net_price`` и служит образцом для focused-теста на побитовое
совпадение с ядром, а также fallback, если расширение не собрано.
"""
import importlib

from ..support import EPS


class NetPriceHooks(object):
    """Побочный эффект, который обязан остаться в Python.

    Хук здесь один: построение котировок покупателей. Он не считает цену —
    только отдаёт уже готовый список пар (order, limit).
    """

    __slots__ = ('engine',)

    def __init__(self, engine):
        self.engine = engine

    def quotes(self, e):
        """Котировки покупателей — прежний ``self.customer_quotes(e)``."""
        return self.engine.customer_quotes(e)


def net_price_python(engine, e, hooks=None):
    """Эталон числового цикла — буквальный прежний код V3.net_price."""
    hooks = hooks or NetPriceHooks(engine)
    consumer = e['good'] in (engine.cfg['basic_needs'] | engine.cfg['extra_needs'])
    quantity, final_quantity = 0.0, 0.0
    price_base = e['price']
    for order, limit in hooks.quotes(e):
        if limit+EPS < price_base:
            continue
        landed = price_base+(order['price']-limit)
        volume = min(order['remaining'], order['reserved']/max(landed, EPS))
        quantity += volume
        if order.get('final_consumption'):
            final_quantity += volume
    share = (final_quantity/quantity if quantity > EPS else float(consumer))
    return price_base*(1-engine.cfg['consumption_tax']*share)


def net_price(engine, e):
    """Единственный вход из ``V3.net_price``: native-цикл или эталонный fallback."""
    consumer = e['good'] in (engine.cfg['basic_needs'] | engine.cfg['extra_needs'])
    return run(e, engine.customer_quotes(e), EPS, engine.cfg['consumption_tax'],
               consumer, e['price'])


def load_net_price_kernel():
    """Вернуть native-модуль net_price_kernel или None, если он не собран.

    Отсутствие собранного расширения даёт ImportError, а не
    ModuleNotFoundError: пакет ``modules.economy.native`` существует, а
    подмодуля нет. Обе ситуации означают одно — ядро не собрано.
    """
    try:
        return importlib.import_module('.net_price_kernel', __package__)
    except (ImportError, ModuleNotFoundError):
        return None


def net_price_reference(engine, e):
    """Эталон целиком через ту же границу — для focused-теста."""
    return net_price_python(engine, e)


NATIVE_KERNEL = False

_kernel = load_net_price_kernel()
if _kernel is not None:
    run = _kernel.net_price
    NATIVE_KERNEL = True
else:
    def run(e, quotes, eps, consumption_tax, consumer, price_base):
        """Python-эквивалент ядра с той же сигнатурой, что у native-функции."""
        quantity, final_quantity = 0.0, 0.0
        for order, limit in quotes:
            if limit+eps < price_base:
                continue
            landed = price_base+(order['price']-limit)
            volume = min(order['remaining'], order['reserved']/max(landed, eps))
            quantity += volume
            if order.get('final_consumption'):
                final_quantity += volume
        share = (final_quantity/quantity if quantity > eps else float(consumer))
        return price_base*(1-consumption_tax*share)
