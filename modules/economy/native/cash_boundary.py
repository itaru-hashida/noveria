"""Граница денежных мутаций: одна привязка реализации на импорт.

Модуль не хранит состояние движка: ``CashStore`` получает живой словарь
``engine.accounts`` и меняет значения тех же словарей. Копий счетов нет ни
здесь, ни в extension, поэтому остальной код продолжает читать счёт как
обычный словарь.

Эталонная Python-реализация живёт в этом же модуле: она нужна как образец для
focused-теста и как fallback, если расширение не собрано. Имена привязываются
один раз при импорте, чтобы не платить за диспетчер на каждой мутации.

Модуль не импортирует ``modules.economy.support``: у границы нет EPS и других
констант проекта, поэтому циклического импорта нет.
"""

import copy
import importlib


class CashStorePython:
    """Эталон границы: колонки денежных полей над теми же живыми счетами.

    Сверка с dict повторяет native-ветку: прямая запись в обход границы
    (native-ветки восстановления предприятий) не должна теряться. Сравнивается
    и тип, потому что ``5`` и ``5.0`` равны по значению, но подмена типа
    сдвинула бы последние биты отчёта.
    """

    def __init__(self, accounts):
        self.accounts = accounts
        self.version = 0
        self.keys = []
        self.balances = []
        self.reserved = []
        self.index = {}
        for account_id in accounts:
            self._append(account_id)
        self.version += 1

    def _append(self, account_id):
        account = self.accounts[account_id]
        self.index[account_id] = len(self.keys)
        self.keys.append(account_id)
        self.balances.append(account['balance'])
        self.reserved.append(account.get('reserved', 0))

    def _load(self):
        self.keys = []
        self.balances = []
        self.reserved = []
        self.index = {}
        for account_id in self.accounts:
            self._append(account_id)
        self.version += 1

    def sync(self):
        total, have = len(self.accounts), len(self.keys)
        if total == have:
            if list(self.accounts) != self.keys:
                self._load()
            return
        if total < have or list(self.accounts)[:have] != self.keys:
            self._load()
            return
        for account_id in list(self.accounts)[have:]:
            self._append(account_id)

    def _slot(self, account_id):
        slot = self.index.get(account_id)
        if slot is not None:
            if slot < len(self.keys) and self.keys[slot] == account_id:
                return slot
            self._load()
            return self.index.get(account_id, -1)
        self.sync()
        return self.index.get(account_id, -1)

    def _known(self, account_id):
        slot = self._slot(account_id)
        if slot < 0:
            raise KeyError(f'Нет счёта в CashStore: {account_id!r}')
        return slot

    @staticmethod
    def _reconcile(column, value):
        current = column
        if current is not value and (current != value
                                    or type(current) is not type(value)):
            return value
        return current

    def get_balance(self, account_id):
        slot = self._known(account_id)
        self.balances[slot] = self._reconcile(
            self.balances[slot], self.accounts[account_id]['balance'])
        return self.balances[slot]

    def set_balance(self, account_id, value):
        slot = self._known(account_id)
        self.balances[slot] = value
        self.accounts[account_id]['balance'] = value
        return value

    def add_balance(self, account_id, value):
        slot = self._known(account_id)
        total = self.get_balance(account_id)+value
        self.balances[slot] = total
        self.accounts[account_id]['balance'] = total
        return total

    def move_balance(self, sender, recipient, amount):
        source, target = self._known(sender), self._known(recipient)
        left = self.get_balance(sender)-amount
        right = self.get_balance(recipient)+amount
        self.balances[source] = left
        self.balances[target] = right
        self.accounts[sender]['balance'] = left
        self.accounts[recipient]['balance'] = right
        return left, right

    def get_reserved_cash(self, account_id):
        slot = self._known(account_id)
        self.reserved[slot] = self._reconcile(
            self.reserved[slot], self.accounts[account_id].get('reserved', 0))
        return self.reserved[slot]

    def set_reserved_cash(self, account_id, value):
        slot = self._known(account_id)
        self.reserved[slot] = value
        self.accounts[account_id]['reserved'] = value
        return value

    def add_reserved_cash(self, account_id, value):
        slot = self._known(account_id)
        total = self.get_reserved_cash(account_id)+value
        self.reserved[slot] = total
        self.accounts[account_id]['reserved'] = total
        return total

    def release_reserved_cash(self, account_id, value):
        """Освободить часть резерва: ``max(0, reserved - value)``."""
        slot = self._known(account_id)
        left = self.get_reserved_cash(account_id)-value
        if not left > 0:
            left = 0
        self.reserved[slot] = left
        self.accounts[account_id]['reserved'] = left
        return left

    def columns(self):
        self.sync()
        return dict(keys=list(self.keys), balance=list(self.balances),
                    reserved=list(self.reserved))

    def __deepcopy__(self, memo):
        return type(self)(copy.deepcopy(self.accounts, memo))

    def __reduce__(self):
        return (type(self), (self.accounts,))


def load_cash_store():
    """Вернуть native-модуль cash_store или None, если он не собран.

    Отсутствие расширения даёт ImportError, а не ModuleNotFoundError: пакет
    ``modules.economy.native`` существует, а подмодуля нет. Обе ситуации
    означают одно — расширение не собрано, берём эталон.
    """
    try:
        return importlib.import_module('.cash_store', __package__)
    except (ImportError, ModuleNotFoundError):
        return None


NATIVE_CASH_STORE = False

_store = load_cash_store()
if _store is not None:
    CashStore = _store.CashStore
    NATIVE_CASH_STORE = True
else:
    CashStore = CashStorePython
