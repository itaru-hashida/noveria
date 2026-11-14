# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Живое представление книги офферов: те же объекты заявок, без копирования.

Книга рынка (``book``) хранит список ``options`` из троек ``(price, sell, quote)``,
где ``sell`` — живой объект заявки: сделка меняет только ``remaining`` и
``status``. Представление обязано читать именно эти объекты во время обхода,
поэтому здесь нет ни снимка, ни собственной копии заявок — только курсор.

Поля ``remaining``/``reserved``/``status``/``price`` принадлежат Cython-колонкам
``OrderStore``. Книга строит номера слотов один раз (``book['slots']``) и
хранит их рядом с тройками: обход читает колонку по номеру и не ищет
``id(order)`` на каждом оффере. Заявка без слота — это снимок
(``_planning_market_snapshot`` копирует заявки), и для неё единственный
источник значений остаётся словарь самой заявки.

Поведение повторяет генератор V3 буквально:

  * ``iter(view)`` каждый раз создаёт новый обход, начиная с сохранённого
    ``book['first_live']`` (курсор по уже выкупленным офферам);
  * скан курсора и захват границы выполняются на первом ``next()``, а не в
    ``iter()``: генератор V3 тоже ленив, и ``first_live`` пишется только тогда,
    когда обход действительно начался;
  * ``remaining``/``status`` перечитываются на каждом шаге, поэтому мутация
    заявки между двумя обходами видна второму обходу;
  * свой оффер продавца исключается, ``local_only`` ограничивает провинцией.

Возвращается та же тройка, что лежит в книге: ``sell`` и ``quote`` — исходные
объекты, поэтому tie-break и landed price остаются такими же, какими их
построила ``_market_book``.

Номера слотов действительны, пока раскладка колонок не сдвинулась. Признак
сдвига — ``store.version``: ``remove`` и пересборка колонок увеличивают его, и
тогда представление пересчитывает номера для книги заново. Дописывание в хвост
номеров не сдвигает, поэтому версия не меняется.
"""

# EPS проекта; задаётся из Python-границы, чтобы не дублировать константу.
cdef double _EPS = 1e-7


def set_epsilon(double eps):
    """Зафиксировать EPS проекта для проверки остатка оффера."""
    global _EPS
    _EPS = eps


cdef inline bint _is_live(object sell, Py_ssize_t slot, object remaining,
                          object status):
    """Живость оффера: остаток выше допуска и статус не снят.

    При неотрицательном слоте значения читаются из колонок хранилища, иначе из
    словаря заявки: у снимка planning-книги колонок нет, и его словарь —
    единственный источник.
    """
    cdef object left, state
    if slot >= 0:
        left = <object>remaining[slot]
        state = <object>status[slot]
    else:
        left = sell['remaining']
        state = sell['status']
    return (left > _EPS
            and state != 'cancelled'
            and state != 'expired')


cdef class _OfferIterator:
    """Один проход по книге: ленивый захват границы и живые проверки."""

    cdef object _book
    cdef object _buyer_id
    cdef object _province_id
    cdef bint _local_only
    cdef object _options
    cdef Py_ssize_t _index
    cdef Py_ssize_t _stop
    cdef bint _started
    cdef bint _finished
    # Колонки и номера слотов берутся из книги один раз на обход, а не на шаг.
    cdef object _slots
    cdef object _remaining
    cdef object _status

    def __cinit__(self, object book, object buyer_id, object province_id,
                  bint local_only):
        self._book = book
        self._buyer_id = buyer_id
        self._province_id = province_id
        self._local_only = local_only
        self._options = None
        self._index = 0
        self._stop = 0
        self._started = False
        self._finished = False
        self._slots = None
        self._remaining = None
        self._status = None

    def __iter__(self):
        return self

    cdef void _bind(self, object options, Py_ssize_t limit):
        """Взять колонки книги и проверить, что номера слотов ещё верны.

        Сверка стоит одно чтение ``version`` на обход. Номера пересчитываются
        только когда раскладка действительно сдвинулась (``remove`` или
        пересборка колонок) либо книга пришла от другого хранилища. Обычный
        случай — номера уже посчитаны при построении книги.
        """
        cdef object store = self._book.get('slot_store')
        cdef object slots
        if store is None:
            self._slots = None
            self._remaining = None
            self._status = None
            return
        slots = self._book.get('slots')
        if (slots is None or len(slots) != limit
                or self._book.get('slot_version') != store.version):
            slots = store.slots([<object>row[1] for row in options])
            self._book['slots'] = slots
            self._book['slot_version'] = store.version
        self._slots = slots
        self._remaining = store.remaining
        self._status = store.status

    cdef Py_ssize_t _slot_at(self, Py_ssize_t index):
        """Номер слота оффера в позиции книги; -1 для снимка без колонок."""
        if self._slots is None:
            return -1
        return <Py_ssize_t>self._slots[index]

    cdef void _begin(self):
        """Ленивый старт прохода: скан курсора и захват границы.

        Выполняется на первом ``next()``, как тело генератора V3. Курсор
        ``first_live`` сохраняется в книге: следующие обходы не просматривают
        уже выкупленные офферы повторно.
        """
        cdef object options = self._book['options']
        cdef Py_ssize_t first = <Py_ssize_t>self._book['first_live']
        cdef Py_ssize_t limit = len(options)
        self._started = True
        if first > limit:
            first = limit
        self._bind(options, limit)
        while first < limit:
            if _is_live(<object>(<object>options[first])[1], self._slot_at(first),
                        self._remaining, self._status):
                break
            first += 1
        self._book['first_live'] = first
        self._options = options
        self._index = first
        self._stop = limit

    def __next__(self):
        cdef object options
        cdef object slots
        cdef object remaining
        cdef object status
        cdef object row
        cdef object sell
        cdef Py_ssize_t slot
        if not self._started:
            self._begin()
        if self._finished:
            raise StopIteration
        options = self._options
        slots = self._slots
        remaining = self._remaining
        status = self._status
        while self._index < self._stop:
            row = <object>options[self._index]
            slot = -1
            if slots is not None:
                slot = <Py_ssize_t>slots[self._index]
            self._index += 1
            sell = <object>row[1]
            if (sell['participant'] != self._buyer_id
                    and _is_live(sell, slot, remaining, status)
                    and (not self._local_only
                         or sell['province_id'] == self._province_id)):
                return row
        self._finished = True
        raise StopIteration


cdef class OfferView:
    """Ленивое представление единой книги без копирования её офферов.

    Держит только ссылку на книгу и параметры покупателя; сами офферы
    остаются живыми объектами книги. Номера слотов и колонки приходят из
    книги, поэтому представление не ищет ``id(order)`` на каждом шаге.
    """

    cdef public object book
    cdef public object buyer_id
    cdef public object province_id
    cdef public bint local_only

    def __cinit__(self, object book, object buyer_id, object province_id,
                  bint local_only=False):
        self.book = book
        self.buyer_id = buyer_id
        self.province_id = province_id
        self.local_only = local_only

    def __iter__(self):
        return _OfferIterator(self.book, self.buyer_id, self.province_id,
                              self.local_only)

    def __bool__(self):
        # Обход начинается, поэтому курсор сдвигается — ровно как в V3,
        # где bool(view) вызывал next(iter(view), None).
        return next(_OfferIterator(self.book, self.buyer_id, self.province_id,
                                   self.local_only), None) is not None

    def __getitem__(self, index):
        cdef Py_ssize_t wanted = index
        cdef Py_ssize_t current = 0
        cdef object iterator
        cdef object row
        if wanted < 0:
            raise IndexError('OfferView не поддерживает отрицательный индекс')
        iterator = _OfferIterator(self.book, self.buyer_id, self.province_id,
                                  self.local_only)
        while True:
            try:
                row = next(iterator)
            except StopIteration:
                raise IndexError(index)
            if current == wanted:
                return row
            current += 1
