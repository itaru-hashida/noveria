"""Граница diagnostics-ядра: одна привязка реализации на импорт.

Ядро считает только числовые counters агрегации сделок для компактного event
trace (``trade.executed``): счётчики и суммы по прежнему ключу группировки и
в прежнем порядке. Сами события создаёт ``Economy.log_event`` — Python:
тексты, форматирование report и порядок событий остаются у движка, поэтому
количество и порядок событий гарантированно прежние.

Числовая проверка инвариантов validate уже перенесена раньше в
settlement_kernel и этот модуль её не дублирует.

Эталонная Python-реализация живёт в этом же модуле: она нужна как образец
для focused-теста на побитовое совпадение и как fallback, если расширение
не собрано. Имена привязываются один раз при импорте.
"""

import importlib
from collections import defaultdict


def aggregate_trade_groups_python(trades):
    """Эталон агрегации — буквальный прежний код ``build_causal_events``."""
    trade_groups = defaultdict(lambda: dict(count=0, quantity=0.0, gold=0.0,
                                            freight=0.0))
    for trade in trades:
        key = (trade.get('buyer'), trade.get('seller'), trade.get('good'),
               trade.get('buyer_province'), trade.get('seller_province'),
               trade.get('purpose'))
        group = trade_groups[key]
        group['count'] += 1
        group['quantity'] += trade.get('quantity', 0)
        group['gold'] += trade.get('gold', 0)
        group['freight'] += trade.get('freight', 0)
    return [(key, group['count'], group['quantity'], group['gold'],
             group['freight']) for key, group in trade_groups.items()]


def load_diagnostics_kernel():
    """Вернуть native-модуль diagnostics_kernel или None.

    Отсутствие расширения даёт ImportError, а не ModuleNotFoundError: пакет
    ``modules.economy.native`` существует, а подмодуля нет. Обе ситуации
    означают одно — расширение не собрано, берём эталон.
    """
    try:
        return importlib.import_module('.diagnostics_kernel', __package__)
    except (ImportError, ModuleNotFoundError):
        return None


NATIVE_KERNEL = False

_kernel = load_diagnostics_kernel()
if _kernel is not None:
    aggregate_trade_groups = _kernel.aggregate_trade_groups
    NATIVE_KERNEL = True
else:
    aggregate_trade_groups = aggregate_trade_groups_python
