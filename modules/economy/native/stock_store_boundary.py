"""Binding and Python fallback for StockStore."""
import importlib


class StockStorePython:
    def __init__(self, accounts):
        self.accounts = accounts
        self.stock = {aid: a['stock'] for aid, a in accounts.items()}
        self.reserved = {aid: a.get('stock_reserved', {}) for aid, a in accounts.items()}

    def _account(self, aid):
        account = self.accounts[aid]
        self.stock[aid] = account['stock']
        self.reserved[aid] = account.get('stock_reserved', {})
        return account

    def get_stock(self, aid, good):
        self._account(aid)
        return self.stock[aid].get(good, 0)

    def set_stock(self, aid, good, value):
        self._account(aid)
        self.stock[aid][good] = value
        return value

    def add_stock(self, aid, good, quantity):
        return self.set_stock(aid, good, self.get_stock(aid, good) + quantity)

    def get_reserved(self, aid, good):
        self._account(aid)
        return self.reserved[aid].get(good, 0)

    def set_reserved(self, aid, good, value):
        self._account(aid)
        if 'stock_reserved' not in self.accounts[aid]:
            self.accounts[aid]['stock_reserved'] = self.reserved[aid] = {}
        self.reserved[aid][good] = value
        return value

    def add_reserved(self, aid, good, quantity):
        return self.set_reserved(aid, good, self.get_reserved(aid, good) + quantity)

    def release_reserved(self, aid, good, quantity):
        return self.set_reserved(aid, good, max(0, self.get_reserved(aid, good)-quantity))

    def reset_reserved(self, aid):
        self._account(aid)
        self.accounts[aid]['stock_reserved'] = self.reserved[aid] = {}
        return self.reserved[aid]

    def ensure_reserved(self, aid):
        self._account(aid)
        if 'stock_reserved' not in self.accounts[aid]:
            self.accounts[aid]['stock_reserved'] = self.reserved[aid] = {}
        return self.reserved[aid]

    def set_value(self, aid, good, value):
        self.accounts[aid]['values'][good] = value

    def snapshot_good(self, aid, good):
        """Снять значения и наличие ключей для отката одной сделки."""
        account = self._account(aid)
        stock = self.stock[aid]
        reserved = self.reserved[aid]
        values = account.get('values', {})
        return (good in stock, stock.get(good), 'stock_reserved' in account,
                good in reserved, reserved.get(good), good in values, values.get(good))

    def restore_good(self, aid, good, snapshot):
        """Восстановить количество, резерв и исходную форму словарей."""
        (stock_present, quantity, has_reserves, reserved_present, reserved,
         value_present, value) = snapshot
        account = self._account(aid)
        if stock_present:
            self.stock[aid][good] = quantity
        else:
            self.stock[aid].pop(good, None)
        if not has_reserves:
            account.pop('stock_reserved', None)
            self.reserved[aid] = {}
        elif reserved_present:
            self.ensure_reserved(aid)[good] = reserved
        else:
            self.ensure_reserved(aid).pop(good, None)
        if value_present:
            self.set_value(aid, good, value)
        elif 'values' in account:
            account['values'].pop(good, None)

    def sync(self):
        self.stock = {aid: a['stock'] for aid, a in self.accounts.items()}
        self.reserved = {aid: a.get('stock_reserved', {}) for aid, a in self.accounts.items()}

    def __deepcopy__(self, memo):
        return type(self)(memo.get(id(self.accounts), self.accounts))


try:
    _kernel = importlib.import_module('.stock_store', __package__)
except (ImportError, ModuleNotFoundError):
    _kernel = None

StockStore = _kernel.StockStore if _kernel is not None else StockStorePython
NATIVE_STOCK_STORE = _kernel is not None


def get_stock_store(owner):
    """Return the store for a real engine or a small fallback-owner fixture."""
    store = getattr(owner, 'stock_store', None)
    if store is not None:
        return store
    store = getattr(owner, '_stock_store', None)
    if store is None or store.accounts is not owner.accounts:
        store = StockStore(owner.accounts)
        try:
            owner._stock_store = store
        except AttributeError:
            pass
    return store
