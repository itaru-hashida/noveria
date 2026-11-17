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

    ``protected`` — отдельная колонка, а не второй ``reserved``: резерв под
    заявки и защита кассы под обязательства считаются независимо. Принятое из
    dict расхождение считается в ``adopt_mismatch``; в ``strict`` оно вместо
    этого падает, потому что обычная мутация обязана идти через владельца.
    """

    def __init__(self, accounts, strict=False):
        self.accounts = accounts
        self.strict = bool(strict)
        self.adopt_mismatch = 0
        self.version = 0
        self.keys = []
        self.balances = []
        self.reserved = []
        self.protected = []
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
        self.protected.append(account.get('protected', 0))

    def _load(self):
        self.keys = []
        self.balances = []
        self.reserved = []
        self.protected = []
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

    def _reconcile(self, column, column_slot, account_id, field, default):
        """Принять значение dict, если оно изменилось в обход границы."""
        account = self.accounts[account_id]
        value = account[field] if default is None else account.get(field, default)
        current = column[column_slot]
        if current is not value and (current != value
                                     or type(current) is not type(value)):
            if self.strict:
                raise ValueError(
                    f'CashStore: {field} счёта {account_id!r} изменён в обход '
                    f'владельца (колонка {current!r}, dict {value!r})')
            column[column_slot] = value
            self.adopt_mismatch += 1
        return column[column_slot]

    def get_balance(self, account_id):
        slot = self._known(account_id)
        return self._reconcile(self.balances, slot, account_id, 'balance', None)

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
        self.balances[source] = left
        self.accounts[sender]['balance'] = left
        # Перевод самому себе считается от результата первой операции: прежний
        # код применял оба присваивания к одному словарю.
        if target == source:
            right = left+amount
        else:
            right = self.get_balance(recipient)+amount
        self.balances[target] = right
        self.accounts[recipient]['balance'] = right
        return left, right

    def get_reserved_cash(self, account_id):
        slot = self._known(account_id)
        return self._reconcile(self.reserved, slot, account_id, 'reserved', 0)

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

    def get_protected_cash(self, account_id):
        slot = self._known(account_id)
        return self._reconcile(self.protected, slot, account_id, 'protected', 0)

    def set_protected_cash(self, account_id, value):
        slot = self._known(account_id)
        self.protected[slot] = value
        self.accounts[account_id]['protected'] = value
        return value

    def add_protected_cash(self, account_id, value):
        slot = self._known(account_id)
        total = self.get_protected_cash(account_id)+value
        self.protected[slot] = total
        self.accounts[account_id]['protected'] = total
        return total

    def release_protected_cash(self, account_id, value):
        """Снять часть защиты: ``max(0, protected - value)``."""
        slot = self._known(account_id)
        left = self.get_protected_cash(account_id)-value
        if not left > 0:
            left = 0
        self.protected[slot] = left
        self.accounts[account_id]['protected'] = left
        return left

    def free_cash(self, account_id):
        """Единственная формула свободной кассы, как в native-хранилище."""
        slot = self._known(account_id)
        balance = self.get_balance(account_id)
        reserved = self.get_reserved_cash(account_id)
        protected = self.get_protected_cash(account_id)
        free = balance-reserved-protected
        if not free > 0.0:
            return 0.0
        return free

    def adopt(self):
        """Явный импорт значений dict во все колонки (load/new-account)."""
        self.sync()
        for slot, account_id in enumerate(self.keys):
            account = self.accounts[account_id]
            self.balances[slot] = account['balance']
            self.reserved[slot] = account.get('reserved', 0)
            self.protected[slot] = account.get('protected', 0)
        return self

    def columns(self):
        self.sync()
        return dict(keys=list(self.keys), balance=list(self.balances),
                    reserved=list(self.reserved), protected=list(self.protected))

    def diagnostics(self):
        return dict(version=int(self.version), accounts=len(self.keys),
                    adopt_mismatch=int(self.adopt_mismatch),
                    strict=bool(self.strict))

    def __deepcopy__(self, memo):
        return type(self)(copy.deepcopy(self.accounts, memo), self.strict)

    def __reduce__(self):
        return (type(self), (self.accounts,))


def store_for(owner, account):
    """Владелец денежных полей для счёта: хранилище движка или граница над dict.

    Обычный путь — ``owner.cash_store``: это живое хранилище движка, и его
    колонки остаются истиной. Заглушки границ (focused-тесты хуков) движка не
    имеют, но приносят тот же живой словарь счёта; для них строится
    ``CashStore`` над этим словарём. Владелец тот же по типу и арифметике, а
    значение попадает в тот же словарь, поэтому второго независимого состояния
    не появляется.
    """
    store = getattr(owner, 'cash_store', None)
    if store is not None:
        return store
    return CashStore({account['id']: account})


def free_cash(store, account_id):
    """Свободная касса через владельца полей: одна формула на весь проект.

    Функция не считает сама: она вызывает ``CashStore.free_cash``, поэтому
    Python-адаптер и native-хранилище дают побитово одно значение. Второй
    реализации формулы быть не должно.
    """
    return store.free_cash(account_id)


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
