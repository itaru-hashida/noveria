# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Единый persistent WorldState: владелец ссылок на уже существующее состояние.

Шаг архитектурный, а не вычислительный. ``WorldState`` не заводит новых полей
экономики и не дублирует данные: он держит ссылки на те же объекты, которыми
движок уже владеет —

    report['orders'] -> OrderStore (колонки заявок)
    accounts         -> CashStore  (колонки денег)
    _native_update_state -> существующий native update state, если он есть

Второй копии нет по построению: ``orders``/``cash`` — это ровно те хранилища,
которые возвращают свойства ``Economy.order_store``/``Economy.cash_store``, а
``native`` — тот же объект, что лежит в ``Economy.__dict__``. Поэтому старый
код, читающий ``economy.order_store``, ``economy.cash_store`` и сам словарь
счетов, продолжает работать без изменений.

``sync`` не считает экономику и не меняет значения игры. Он сверяет тождества
Python-контейнеров: если список заявок или словарь счетов тот же, часть
переиспользуется; если движок пересобрал контейнер (``step`` сортирует счета и
начинает новый отчёт), перепривязывается только изменившаяся часть. Native
update state не пересоздаётся: его раскладку ведёт сам update state, а
``WorldState`` лишь принимает ссылку на текущий объект.

``__deepcopy__`` отдаёт пустой контейнер. Это важно: копия движка получает
собственные ``report['orders']`` и ``accounts``, и ``WorldState`` копии обязан
привязаться к ним при первом обращении, а не утащить хранилища оригинала.
"""


cdef class WorldState:
    """Ссылки на живые хранилища плюс метка раскладки, к которой они привязаны.

    Поля ``orders``/``cash``/``native`` — только ссылки. Значения заявок и
    денег живут в ``OrderStore``/``CashStore`` поверх тех же Python-объектов,
    поэтому здесь нет ни второго списка заявок, ни второй карты счетов.
    """

    cdef public object orders
    cdef public object cash
    cdef public object native
    cdef object _orders_source
    cdef object _accounts_source
    cdef public unsigned long version

    def __cinit__(self):
        self.orders = None
        self.cash = None
        self.native = None
        self._orders_source = None
        self._accounts_source = None
        self.version = 0

    @classmethod
    def from_economy(cls, economy):
        """Построить контейнер по текущему состоянию Economy (без копирования)."""
        state = cls()
        state.sync(economy)
        return state

    def sync(self, economy):
        """Перепривязать изменившиеся части; неизменившиеся переиспользовать.

        Сверка идёт по тождеству контейнеров, а не по значениям: сравнение
        содержимого стоило бы прохода по всем заявкам и счетам, а раскладка
        меняется только заменой самого объекта. Значения игры метод не трогает:
        он лишь берёт ссылки на существующие хранилища движка.

        Дополнительно проверяется, что движок не подменил собственный кэш
        границы (``_order_store``/``_cash_store``) при том же контейнере: тогда
        контейнер переиспользует новый кэш движка, а не устаревшую ссылку.
        """
        cdef object orders_source = economy.report['orders']
        cdef object accounts_source = economy.accounts
        cdef object store
        cdef bint changed = False
        if self.orders is None or self._orders_source is not orders_source:
            self.orders = economy.order_store
            self._orders_source = orders_source
            changed = True
        else:
            store = economy.__dict__.get('_order_store')
            if store is not None and store is not self.orders:
                self.orders = store
                changed = True
        if self.cash is None or self._accounts_source is not accounts_source:
            self.cash = economy.cash_store
            self._accounts_source = accounts_source
            changed = True
        else:
            store = economy.__dict__.get('_cash_store')
            if store is not None and store is not self.cash:
                self.cash = store
                changed = True
        store = economy.__dict__.get('_native_update_state')
        if store is not self.native:
            self.native = store
            changed = True
        if changed:
            self.version += 1
        return self

    def reset(self):
        """Забыть привязки, не трогая значения игры.

        Хранилища и native state остаются жить в Economy: метод лишь снимает
        ссылки, чтобы следующий ``sync`` привязался заново. Очистка заявок или
        счетов здесь не делается — это не владелец данных.
        """
        self.orders = None
        self.cash = None
        self.native = None
        self._orders_source = None
        self._accounts_source = None
        self.version += 1
        return self

    def snapshot_metadata(self):
        """Иммутабельная сводка раскладки: без выдачи живых внутренних объектов.

        Возвращаются только числа и заново собранные неизменяемые кортежи.
        Ни списки заявок, ни словари счетов, ни ``layout_key`` native state
        наружу не отдаются: вызывающий код не должен получить ручку на живое
        состояние через диагностический снимок.
        """
        cdef object orders = self.orders
        cdef object cash = self.cash
        cdef object native = self.native
        return {
            'version': int(self.version),
            'orders': {
                'bound': orders is not None,
                'count': 0 if orders is None else len(orders.orders),
                'columns': 0 if orders is None else len(orders.remaining),
                'layout_version': 0 if orders is None else int(orders.version),
            },
            'accounts': {
                'bound': cash is not None,
                'count': 0 if cash is None else len(cash.accounts),
                'columns': 0 if cash is None else len(cash.keys),
                'layout_version': 0 if cash is None else int(cash.version),
            },
            'native_update_state': {
                'bound': native is not None,
                'layout': _frozen_layout(None if native is None
                                         else getattr(native, 'layout_key', None)),
            },
        }

    def __deepcopy__(self, memo):
        """Независимый пустой контейнер: копия привяжется к своим контейнерам.

        Копировать хранилища нельзя: у копии движка свои ``report['orders']`` и
        ``accounts``, и унаследованная ссылка была бы вторым независимым
        состоянием. Пустой контейнер привязывается при первом обращении.
        """
        return WorldState()

    def __reduce__(self):
        """Pickle: восстановить пустой контейнер и привязать его при обращении."""
        return (WorldState, ())


def _frozen_layout(object layout):
    """Копия раскладки native state в неизменяемые кортежи (или None)."""
    if layout is None:
        return None
    if isinstance(layout, tuple):
        return tuple(tuple(item) if isinstance(item, (tuple, list)) else item
                     for item in layout)
    return repr(layout)
