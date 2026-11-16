"""Граница enterprise reset-ядра: один вызов из V3.step, эталон и fallback.

Числовой месячный сброс полей предприятий (revenue..equipment_wear и снятие
production_observation) перенесён в Cython. Порядок обхода списка ``existing``,
RNG, report и хуки остаются в Python: ядро получает готовый список и только
пишет сброшенные поля в том же порядке ключей, что и прежний код V3, не
трогая поля вне списка сброса.

Эталонная Python-ветка (``reset_enterprises_python``) повторяет прежний цикл
буквально и служит образцом для focused-теста на побитовое совпадение с
ядром, а также fallback, если расширение не собрано.
"""
import importlib


def reset_enterprises_python(existing):
    """Эталон месячного сброса — буквальный прежний код V3."""
    for e in existing:
        e.pop('production_observation', None)
        e.update(revenue=0, tax=0, expenses=0, profit=0, output=0, workers=0,
                 wages=0, cost_of_sales=0, production_cost=0, input_limited=False,
                 fixed_cost_due=0, fixed_cost_paid=0, dividends=0.0,
                 working_reserve=0.0, project_reserve=0.0, equipment_wear={})


def reset_enterprises(existing):
    """Единственный вход из V3.step: сброс готового списка предприятий."""
    run_reset(existing)


def load_enterprise_reset_kernel():
    """Вернуть native-модуль enterprise_reset_kernel или None, если не собран.

    Отсутствие собранного расширения даёт ImportError, а не
    ModuleNotFoundError: пакет ``modules.economy.native`` существует, а
    подмодуля нет. Обе ситуации означают одно — ядро не собрано.
    """
    try:
        return importlib.import_module('.enterprise_reset_kernel', __package__)
    except (ImportError, ModuleNotFoundError):
        return None


NATIVE_KERNEL = False

_kernel = load_enterprise_reset_kernel()
if _kernel is not None:
    run_reset = _kernel.run_reset
    NATIVE_KERNEL = True
else:
    run_reset = reset_enterprises_python
