# cython: language_level=3, boundscheck=False, wraparound=False
"""Cython-owned stock and stock-reservation columns."""

cdef class StockStore:
    cdef public dict accounts
    cdef public dict stock
    cdef public dict reserved

    def __cinit__(self, object accounts):
        self.accounts = accounts
        self.stock = {}
        self.reserved = {}
        self._load()

    cdef _load(self):
        cdef object aid, account
        self.stock = {}
        self.reserved = {}
        for aid, account in self.accounts.items():
            self.stock[aid] = account['stock']
            self.reserved[aid] = account.get('stock_reserved', {})

    cdef _account(self, object aid):
        cdef object current
        if aid not in self.accounts:
            raise KeyError(aid)
        if self.stock.get(aid) is not self.accounts[aid]['stock']:
            self.stock[aid] = self.accounts[aid]['stock']
        current = self.accounts[aid].get('stock_reserved', {})
        if self.reserved.get(aid) is not current:
            self.reserved[aid] = current
        return self.accounts[aid]

    def get_stock(self, aid, good):
        self._account(aid)
        return self.stock[aid].get(good, 0)

    def set_stock(self, aid, good, value):
        self._account(aid)
        self.stock[aid][good] = value
        return value

    def add_stock(self, aid, good, quantity):
        value = self.get_stock(aid, good) + quantity
        return self.set_stock(aid, good, value)

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
        return self.set_reserved(aid, good,
                                 self.get_reserved(aid, good) + quantity)

    def release_reserved(self, aid, good, quantity):
        return self.set_reserved(aid, good,
                                 max(0, self.get_reserved(aid, good)-quantity))

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
        self._load()

    def __deepcopy__(self, memo):
        return type(self)(memo.get(id(self.accounts), self.accounts))

    def __reduce__(self):
        """Восстановить store над тем же словарём счетов при pickle."""
        return type(self), (self.accounts,)
