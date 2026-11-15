"""Граница consumption-ядра: один вызов из V3.step, эталон и fallback.

В Cython уходит только числовой цикл потребления домохозяйств: обход готового
списка домохозяйств, обход их ``needs`` и расчёт
``consumed = min(needed, free_stock)``. Всё остальное остаётся Python-хуками:

  * списание со склада — через существующий ``engine.use_stock``, который
    делегирует в stock boundary; второй копии склада здесь нет;
  * ``need.update(consumed=..., deficit=...)`` — запись результата в need;
  * ``report['consumed'][good]`` — накопление отчёта месяца.

Порядок обхода сохраняется буквально: список домохозяйств готовит Python
(``native_households`` в cython-режиме либо ``engine.shuffled``), порядок
товаров задаёт порядок вставки в ``needs``. RNG граница не трогает.

Эталонная Python-ветка (``run_households_python``) повторяет прежнее тело
цикла из ``Economy.step`` и служит образцом для focused-теста на побитовое
совпадение с ядром, а также fallback, если расширение не собрано.
"""
import importlib


class ConsumptionHooks(object):
    """Побочные эффекты consumption, которые обязаны остаться в Python.

    Ядро вызывает ``consume`` в том же порядке, в каком прежний цикл писал
    склад, need и отчёт. Ни один метод не считает норму потребления — только
    фиксирует уже посчитанное значение.
    """

    __slots__ = ('engine',)

    def __init__(self, engine):
        self.engine = engine

    def consume(self, account, good, need, consumed):
        """Списать ``consumed`` со склада и записать результат в need/отчёт."""
        engine = self.engine
        engine.use_stock(account['id'], good, consumed)
        # Порядок аргументов и чтение need['needed'] — как в прежнем коде.
        need.update(consumed=consumed, deficit=need['needed']-consumed)
        report = engine.report['consumed']
        report[good] = report.get(good, 0)+consumed


def run_households_python(households, engine, hooks):
    """Эталон числового цикла consumption — буквальный прежний код V3.step."""
    for a in households:
        for good, need in a.get('needs', {}).items():
            needed = need['needed']
            consumed = min(needed, engine.free_stock(a['id'], good))
            hooks.consume(a, good, need, consumed)


def consume_households(engine, households):
    """Единственный вход из V3.step: native-цикл или эталонный fallback."""
    run_households(households, engine, ConsumptionHooks(engine))


def consume_households_python(engine, households):
    """Эталонная Python-ветка целиком: тот же список и те же хуки."""
    run_households_python(households, engine, ConsumptionHooks(engine))


def load_consumption_kernel():
    """Вернуть native-модуль consumption_kernel или None, если он не собран.

    Отсутствие собранного расширения даёт ImportError, а не
    ModuleNotFoundError: пакет ``modules.economy.native`` существует, а
    подмодуля нет. Обе ситуации означают одно — ядро не собрано.
    """
    try:
        return importlib.import_module('.consumption_kernel', __package__)
    except (ImportError, ModuleNotFoundError):
        return None


NATIVE_KERNEL = False

_kernel = load_consumption_kernel()
if _kernel is not None:
    run_households = _kernel.run_households
    NATIVE_KERNEL = True
else:
    run_households = run_households_python
