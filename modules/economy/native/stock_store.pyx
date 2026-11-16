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

    def sync(self):
        self._load()

    def __deepcopy__(self, memo):
        return type(self)(memo.get(id(self.accounts), self.accounts))
