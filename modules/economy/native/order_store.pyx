# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Cython-owned поля заявок: колонки — источник истины, dict — compatibility view.

Хранилище импортирует живой список ``report['orders']`` и держит значения
``remaining``/``reserved``/``status``/``price`` в параллельных колонках. Дальше
истина живёт в колонках: ``set_*`` пишет колонку, а затем повторяет значение в
Python-dict, чтобы весь остальной код продолжал читать заявку как обычный
словарь. Второго независимо изменяемого состояния нет: dict — это отражение
колонок, и запись идёт только через одну из операций ниже.

Колонки — списки Python-объектов, а не типизированные массивы. Это требование
точности: ``remaining``/``reserved`` бывают и ``int``, и ``float``, а блочная
запись в ``double`` поменяла бы и тип, и последний разряд, от которого зависят
порядок сделок и RNG. Значение кладётся в колонку как есть.

Список заявок может вырасти в обход границы: так делают ветки
``native_mode='cython'``, которые материализуют заявки прямо в
``engine.report['orders']`` (эти файлы вне разрешённого участка правки).
Поэтому каждая операция сначала выравнивает хвост колонок по списку: новые
заявки импортируются из своих словарей. Это дешёвая проверка длин, а не скан.

Слот ищется по ``id(order)`` и **обязательно** проверяется тождеством
``orders[slot] is order``. Одного ``id()`` мало: после сборки мусора адрес
объекта переиспользуется, и устаревший слот указал бы на чужую заявку. При
``boundscheck=False`` такая ошибка была бы чтением за границей списка, а не
исключением, поэтому проверка обязательна.

Чтение для ``OfferView`` сделано без вызова Python-метода на каждый оффер:
``_OfferIterator`` берёт у хранилища ссылки на ``index``/``remaining``/``status``
один раз на обход и дальше читает колонки напрямую. Поэтому представление
видит мутацию сразу и не платит за границу на каждом шаге.
"""

import copy

# Допуск остатка для is_live; задаётся из Python-границы, чтобы не дублировать
# константу проекта. Сами мутации EPS не используют: арифметику считает
# вызывающий код, а граница только записывает точное значение.
cdef double _EPS = 1e-7


def set_epsilon(double eps):
    """Зафиксировать EPS проекта для проверки живости оффера."""
    global _EPS
    _EPS = eps


cdef class OrderStore:
    """Колонки полей заявок плюс живой список dict-ов как compatibility view.

    ``orders`` — исходный список движка (``report['orders']``), а не копия.
    ``index`` отображает ``id(order)`` в номер слота; слот всегда проверяется
    тождеством объекта, потому что ``id`` переиспользуется после сборки мусора.
    """

    cdef public object orders
    cdef public list remaining
    cdef public list reserved
    cdef public list status
    cdef public list price
    cdef public dict index
    cdef public unsigned long version
    cdef object _memo_order
    cdef Py_ssize_t _memo_slot

    def __cinit__(self, object orders):
        self.orders = orders
        # Номер раскладки. Меняется, когда слоты заявок сдвигаются (remove или
        # пересборка колонок). Кэш слотов в книге сверяется с ним, поэтому
        # устаревшие номера не переживут структурную правку.
        self.version = 0
        # Последний найденный слот. Цикл сделки пишет одну и ту же заявку
        # несколько раз подряд, поэтому один identity-сравнение дешевле, чем
        # id()+поиск в индексе на каждую мутацию.
        self._memo_order = None
        self._memo_slot = -1
        self._load()

    # --- импорт текущих заявок --------------------------------------------
    cdef int _load(self) except -1:
        """Считать колонки из словарей: единственный импорт значений."""
        cdef Py_ssize_t i, total = len(self.orders)
        cdef object order
        self.remaining = [None]*total
        self.reserved = [None]*total
        self.status = [None]*total
        self.price = [None]*total
        self.index = {}
        for i, order in enumerate(self.orders):
            self._import(i, order)
        self.version += 1
        return 0

    cdef int _import(self, Py_ssize_t slot, object order) except -1:
        """Импортировать поля одной заявки в колонки и запомнить слот."""
        self.remaining[slot] = order['remaining']
        self.reserved[slot] = order.get('reserved', 0)
        self.status[slot] = order['status']
        self.price[slot] = order.get('price', 0)
        self.index[id(order)] = slot
        return 0

    cdef int _sync(self) except -1:
        """Выровнять колонки по списку заявок после внешнего append.

        Ветки ``native_mode='cython'`` добавляют заявки прямо в список движка.
        Проверка длин стоит две загрузки и в обычном случае сразу выходит.
        """
        cdef Py_ssize_t have = len(self.remaining)
        cdef Py_ssize_t total = len(self.orders)
        cdef Py_ssize_t i
        cdef object order
        if total == have:
            return 0
        if total < have:
            # Список укоротили в обход границы: пересобрать колонки целиком.
            return self._load()
        for i in range(have, total):
            order = <object>self.orders[i]
            self.remaining.append(order['remaining'])
            self.reserved.append(order.get('reserved', 0))
            self.status.append(order['status'])
            self.price.append(order.get('price', 0))
            self.index[id(order)] = i
        # Дописывание в хвост не сдвигает существующие номера, поэтому версия
        # раскладки не меняется: иначе кэш слотов в книгах пересчитывался бы
        # после каждой новой заявки (их ~1,5 тысячи за месяц).
        return 0

    cdef Py_ssize_t _slot(self, object order):
        """Номер слота заявки или -1, если её нет среди живых заявок.

        Горячий путь — одно чтение ``index`` и проверка тождества:
        ``id`` переиспользуется после сборки мусора, поэтому слот обязан
        указывать именно на эту заявку, иначе запись ушла бы в чужую заявку,
        а при ``boundscheck=False`` чтение колонки вышло бы за границу списка.

        Выравнивание колонок по списку делается только при промахе. Это важно
        для скорости: сеттеров за месяц ~10 тысяч, и проверка длин на каждом
        съедала бы больше, чем сама запись. Промах бывает один раз на заявку,
        добавленную в список в обход границы.
        """
        cdef Py_ssize_t slot, total
        cdef object found
        # Горячий путь: та же заявка, что и в прошлый раз (цикл сделки).
        if order is self._memo_order:
            return self._memo_slot
        found = self.index.get(id(order))
        total = len(self.orders)
        if found is not None:
            slot = <Py_ssize_t>found
            if slot < total and <object>self.orders[slot] is order:
                self._memo_order = order
                self._memo_slot = slot
                return slot
        # Промах: либо заявка новая (внешний append), либо индекс устарел.
        self._sync()
        found = self.index.get(id(order))
        if found is None:
            self._memo_order = None
            self._memo_slot = -1
            return -1
        slot = <Py_ssize_t>found
        total = len(self.orders)
        if slot < 0 or slot >= total:
            self._memo_order = None
            self._memo_slot = -1
            return -1
        if <object>self.orders[slot] is not order:
            slot = self._rescan(order)
        self._memo_order = order if slot >= 0 else None
        self._memo_slot = slot
        return slot

    cdef Py_ssize_t _rescan(self, object order):
        """Медленный путь: найти заявку по списку и починить индекс.

        Нужен только после переиспользования ``id`` (объект удалён, адрес
        достался новой заявке). За месяц это единицы вызовов.
        """
        cdef Py_ssize_t i, total = len(self.orders)
        for i in range(total):
            if <object>self.orders[i] is order:
                self.index[id(order)] = i
                return i
        self.index.pop(id(order), None)
        return -1

    cdef int _reindex(self, Py_ssize_t start) except -1:
        """Пересобрать index после удаления: номера сдвинулись."""
        cdef Py_ssize_t i, total = len(self.orders)
        cdef object order
        for i in range(start, total):
            order = <object>self.orders[i]
            self.index[id(order)] = i
        return 0

    # --- состав списка -----------------------------------------------------
    def append(self, object order):
        """Добавить заявку: слот колонок берётся из её текущих значений."""
        self._sync()
        self.orders.append(order)
        self.remaining.append(order['remaining'])
        self.reserved.append(order.get('reserved', 0))
        self.status.append(order['status'])
        self.price.append(order.get('price', 0))
        self.index[id(order)] = len(self.orders)-1
        # Хвостовое дописывание не сдвигает номера: версия не меняется.
        self._memo_order = order
        self._memo_slot = len(self.orders)-1

    def remove(self, object order):
        """Убрать заявку: слот удаляется из всех колонок, хвост переиндексируется."""
        cdef Py_ssize_t slot = self._slot(order)
        if slot < 0:
            raise ValueError('Заявка не принадлежит хранилищу')
        self.orders.pop(slot)
        self.remaining.pop(slot)
        self.reserved.pop(slot)
        self.status.pop(slot)
        self.price.pop(slot)
        self.index.pop(id(order), None)
        self._reindex(slot)
        # Номера заявок сдвинулись: кэш раскладки в книгах недействителен.
        self.version += 1

    # --- поля заявки -------------------------------------------------------
    def set_remaining(self, object order, object value):
        cdef Py_ssize_t slot = self._slot(order)
        if slot >= 0:
            self.remaining[slot] = value
        order['remaining'] = value

    def set_reserved(self, object order, object value):
        cdef Py_ssize_t slot = self._slot(order)
        if slot >= 0:
            self.reserved[slot] = value
        order['reserved'] = value

    def set_status(self, object order, object value):
        cdef Py_ssize_t slot = self._slot(order)
        if slot >= 0:
            self.status[slot] = value
        order['status'] = value

    def set_price(self, object order, object value):
        cdef Py_ssize_t slot = self._slot(order)
        if slot >= 0:
            self.price[slot] = value
        order['price'] = value

    # --- чтение для представления и проверок ------------------------------
    cpdef bint is_live(self, object order, double eps):
        """Живость оффера по колонкам, иначе по словарю заявки."""
        cdef Py_ssize_t slot = self._slot(order)
        if slot < 0:
            return (order['remaining'] > eps
                    and order['status'] != 'cancelled'
                    and order['status'] != 'expired')
        return ((<object>self.remaining[slot]) > eps
                and (<object>self.status[slot]) != 'cancelled'
                and (<object>self.status[slot]) != 'expired')

    def sync(self):
        """Выровнять колонки по списку заявок после внешнего append."""
        self._sync()

    def columns(self):
        """Снимок колонок для тестов и диагностики (без записи в хранилище)."""
        self._sync()
        return dict(remaining=list(self.remaining), reserved=list(self.reserved),
                    status=list(self.status), price=list(self.price))

    # --- копии -------------------------------------------------------------
    def __deepcopy__(self, memo):
        """Копия движка: колонки пересобираются из скопированных заявок.

        Через ``memo`` список заявок остаётся тем же объектом, что и
        ``report['orders']`` копии, поэтому у копии одно хранилище.
        """
        return OrderStore(copy.deepcopy(self.orders, memo))

    def __reduce__(self):
        """Pickle: восстановить хранилище над списком заявок."""
        return (OrderStore, (self.orders,))
