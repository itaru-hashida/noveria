# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Живое представление книги офферов: те же объекты заявок, без копирования.

Книга рынка (``book``) хранит список ``options`` из троек ``(price, sell, quote)``,
где ``sell`` — живой объект заявки: сделка меняет только ``remaining`` и
``status``. Представление обязано читать именно эти объекты во время обхода,
поэтому здесь нет ни снимка, ни собственной копии заявок — только курсор.

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
"""

# EPS проекта; задаётся из Python-границы, чтобы не дублировать константу.
cdef double _EPS = 1e-7


def set_epsilon(double eps):
    """Зафиксировать EPS проекта для проверки остатка оффера."""
    global _EPS
    _EPS = eps


cdef inline bint _is_live(object sell):
    """Живость оффера: остаток выше допуска и статус не снят.

    Значения читаются из живой заявки, а не из снимка: этим представление
    и отличается от копии книги.
    """
    return (sell['remaining'] > _EPS
            and sell['status'] != 'cancelled'
            and sell['status'] != 'expired')


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

    def __iter__(self):
        return self

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
        while first < limit:
            if _is_live(<object>(<object>options[first])[1]):
                break
            first += 1
        self._book['first_live'] = first
        self._options = options
        self._index = first
        self._stop = limit

    def __next__(self):
        cdef object options
        cdef object row
        cdef object sell
        if not self._started:
            self._begin()
        if self._finished:
            raise StopIteration
        options = self._options
        while self._index < self._stop:
            row = <object>options[self._index]
            self._index += 1
            sell = <object>row[1]
            if (sell['participant'] != self._buyer_id
                    and _is_live(sell)
                    and (not self._local_only
                         or sell['province_id'] == self._province_id)):
                return row
        self._finished = True
        raise StopIteration


cdef class OfferView:
    """Ленивое представление единой книги без копирования её офферов.

    Держит только ссылку на книгу и параметры покупателя; сами офферы
    остаются живыми объектами книги.
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
