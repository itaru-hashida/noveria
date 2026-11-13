"""Граница stock-ядра: одна привязка реализации на импорт, без диспетчера.

Модуль намеренно не хранит состояние на движке: ``_physical_stock_totals``
пересоздаётся каждый месяц, а тесты массово копируют движок через deepcopy.
Native-ядро читает словари счёта по владельцу, поэтому копия движка работает
со своей копией склада без перепривязки.

Имена ``add_stock``/``use_stock`` привязываются один раз при импорте. Вызов
через промежуточный диспетчер добавлял бы лишний Python-фрейм на каждую
операцию и съедал бы весь выигрыш ядра.

Модуль не импортирует ``modules.economy.support``: EPS приходит через
``configure()`` из support.py, иначе получается циклический импорт
(support -> boundary -> support).

Если расширение не собрано, используются эталонные Python-функции этой же
границы; они же служат образцом для focused-теста на побитовое совпадение.
"""
import importlib

# Допуск остатка. Переопределяется configure() из support.py, чтобы не
# дублировать константу проекта и не платить за передачу EPS на каждом вызове.
_EPS = 1e-7


def configure(eps):
    """Задать EPS проекта для native-ядра и эталонных Python-функций."""
    global _EPS
    _EPS = eps
    if _kernel is not None:
        _kernel.set_epsilon(eps)


def add_stock_python(owner, account, good, quantity, value=0):
    """Эталон V3 add_stock (EconomyServices.add_stock) с трекингом индекса."""
    account_data = owner.accounts[account]
    before = account_data['stock'].get(good, 0)
    account_data['stock'][good] = before+quantity
    account_data['values'][good] = account_data['values'].get(good, 0)+value
    _update_physical_stock_total(owner, good, before, account_data['stock'][good])


def use_stock_python(owner, account, good, quantity):
    """Эталон V3 use_stock: проверка остатка, средняя стоимость, списание."""
    account_data = owner.accounts[account]
    available = account_data['stock'].get(good, 0)
    held = account_data['values'].get(good, 0)
    if quantity < -_EPS or quantity > available+_EPS:
        raise ValueError('Двойное расходование товара')
    quantity = min(quantity, available)
    value = held*quantity/available if available > 0 else 0
    account_data['stock'][good] = max(0, available-quantity)
    account_data['values'][good] = max(0, held-value)
    _update_physical_stock_total(owner, good, available, account_data['stock'][good])
    return value


def _update_physical_stock_total(owner, good, before, after):
    """Обновить ленивый индекс физических запасов, если он уже построен."""
    totals = getattr(owner, '_physical_stock_totals', None)
    if totals is None or good not in totals:
        return
    row = totals[good]
    row['quantity'] += after-before
    row['count'] += int(after > _EPS)-int(before > _EPS)


def load_stock_kernel():
    """Вернуть native-модуль stock_kernel или None, если он не собран.

    Отсутствие собранного расширения даёт ImportError, а не ModuleNotFoundError:
    пакет ``modules.economy.native`` существует, а подмодуля нет. Обе ситуации
    означают одно — ядро не собрано, и надо взять эталонную Python-ветку.
    """
    try:
        return importlib.import_module('.stock_kernel', __package__)
    except (ImportError, ModuleNotFoundError):
        return None


NATIVE_KERNEL = False

_kernel = load_stock_kernel()
if _kernel is not None:
    add_stock = _kernel.add_stock
    use_stock = _kernel.use_stock
    NATIVE_KERNEL = True
else:
    add_stock = add_stock_python
    use_stock = use_stock_python
