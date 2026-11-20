"""Подготовка и применение потребления в порядке домохозяйств и товаров."""
import importlib


class ConsumptionHooks(object):
    """Побочные эффекты consumption, которые обязаны остаться в Python.

    Граница вызывает ``consume`` в том же порядке, в каком прежний цикл писал
    склад, need и отчёт. Ни один метод не считает норму потребления — только
    фиксирует уже посчитанное значение.
    """

    __slots__ = ('engine',)

    def __init__(self, engine):
        self.engine = engine

    def apply_consumption(self, account, good, need, consumed):
        """Применить рассчитанное потребление через владельца склада."""
        engine = self.engine
        engine.use_stock(account['id'], good, consumed)
        # Порядок аргументов и чтение need['needed'] — как в прежнем коде.
        need.update(consumed=consumed, deficit=need['needed']-consumed)
        report = engine.report['consumed']
        report[good] = report.get(good, 0)+consumed


def run_households(households, engine, hooks, calculate):
    """Считать доступный остаток перед каждым списанием."""
    for a in households:
        for good, need in a.get('needs', {}).items():
            needed = need['needed']
            consumed = calculate(needed, engine.free_stock(a['id'], good))
            hooks.apply_consumption(a, good, need, consumed)


def consume_households(engine, households):
    """Единственный вход из V3.step."""
    run_households(households, engine, ConsumptionHooks(engine), calculate_consumption)


def consume_households_python(engine, households):
    """Эталонная Python-ветка целиком: тот же список и те же хуки."""
    run_households(households, engine, ConsumptionHooks(engine), min)


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


_kernel = load_consumption_kernel()
calculate_consumption = getattr(_kernel, 'calculate_consumption', min)
NATIVE_KERNEL = calculate_consumption is not min
