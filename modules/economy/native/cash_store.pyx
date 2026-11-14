# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Cython-owned денежные поля счетов: колонки — истина, dict — compatibility view.

Хранилище импортирует живой словарь ``engine.accounts`` и держит ``balance`` и
``reserved`` в параллельных колонках. Дальше истина живёт в колонках: каждая
операция пишет колонку, а затем повторяет значение в Python-dict, чтобы весь
остальной код продолжал читать счёт как обычный словарь. Второго независимо
изменяемого состояния нет: dict — это отражение колонок.

Колонки — списки Python-объектов, а не типизированные массивы. Это требование
точности: ``balance``/``reserved`` бывают и ``int``, и ``float``, а приведение к
``double`` поменяло бы тип и последний разряд, от которого зависят порядок
сделок и RNG. Значение кладётся в колонку как есть, арифметика выполняется
обычным Python-сложением над ``object``, поэтому порядок операций тот же, что
и в прежнем коде: ``balance - amount``, ``reserved + extra``.

Словарь счетов может вырасти в обход границы: ``open_enterprise`` и
``sync_locations`` создают счета прямо в ``engine.accounts``. Поэтому любая
операция сначала выравнивает хвост колонок по словарю: новый счёт
импортируется из своего словаря. Это проверка длин, а не скан.

Обратная ситуация — прямая запись в dict в обход границы. Так делают
native-ветки восстановления предприятий (``update_prices_boundary``, файл вне
разрешённого участка правки): они меняют ``balance`` напрямую. Чтобы не
завести второе независимое состояние, каждая мутация и чтение сверяют колонку
с dict и, если dict новее, принимают его значение. В обычном пути значения
равны, поэтому сверка стоит одного чтения и не меняет арифметику.
"""

import copy


cdef class CashStore:
    """Колонки денежных полей плюс живой словарь счетов как compatibility view.

    ``accounts`` — исходный словарь движка, а не копия. ``index`` отображает
    ``account_id`` в номер слота; идентификатор счёта — строка и не
    переиспользуется, поэтому проверки тождества объекта, как в ``OrderStore``,
    не нужны. Слоты стабильны, пока словарь не заменён целиком; замену ловит
    граница движка по тождеству словаря, а рост — ``_sync``.
    """

    cdef public dict accounts
    cdef public list keys
    cdef public list balances
    cdef public list reserved
    cdef public dict index
    cdef public unsigned long version

    def __cinit__(self, object accounts):
        self.accounts = accounts
        # Номер раскладки. Меняется при пересборке колонок; нужен диагностике и
        # тестам, чтобы отличить импорт от продолжения работы.
        self.version = 0
        self._load()

    # --- импорт текущих значений ------------------------------------------
    cdef int _load(self) except -1:
        """Считать колонки из словаря: единственный импорт значений."""
        cdef object account_id
        self.keys = []
        self.balances = []
        self.reserved = []
        self.index = {}
        for account_id in self.accounts:
            self._append(account_id)
        self.version += 1
        return 0

    cdef int _append(self, object account_id) except -1:
        """Добавить счёт в хвост колонок из его словаря."""
        cdef object account = self.accounts[account_id]
        self.index[account_id] = len(self.keys)
        self.keys.append(account_id)
        self.balances.append(account['balance'])
        self.reserved.append(account.get('reserved', 0))
        return 0

    cdef int _sync(self) except -1:
        """Выровнять колонки по словарю счетов после внешнего роста.

        Счета создаются в обход границы при открытии предприятия и при
        синхронизации локаций. Проверка длин стоит две загрузки; если словарь
        заменили целиком или пересортировали, колонки пересобираются, потому
        что слоты тогда недействительны.
        """
        cdef Py_ssize_t have = len(self.keys), total = len(self.accounts), i
        cdef list current
        if total == have:
            return 0
        if total < have:
            return self._load()
        current = list(self.accounts)
        if current[:have] != self.keys:
            return self._load()
        for i in range(have, total):
            self._append(current[i])
        return 0

    cdef int _verify(self) except -1:
        """Сверить порядок колонок со словарём и пересобрать при расхождении.

        ``step`` пересобирает ``self.accounts`` сортировкой, и граница движка
        создаёт новое хранилище по тождеству словаря. Но словарь могут
        пересортировать и на месте (например, в тесте или при ручной загрузке),
        поэтому публичные ``sync``/``columns`` сверяют порядок ключей: это не
        горячий путь, а диагностическая проверка раскладки.
        """
        cdef list current = list(self.accounts)
        if current != self.keys:
            return self._load()
        return 0

    cdef Py_ssize_t _slot(self, object account_id) except -2:
        """Номер слота счёта или -1, если такого счёта нет.

        Слот проверяется значением ключа: словарь могут пересортировать на
        месте, и тогда сохранённый номер укажет на другой счёт. Строки
        идентификаторов не переиспользуются, поэтому сравнения по значению
        достаточно.
        """
        cdef object found = self.index.get(account_id)
        cdef Py_ssize_t slot
        if found is not None:
            slot = <Py_ssize_t>found
            if slot < len(self.keys) and (<object>self.keys[slot]) == account_id:
                return slot
            # Раскладку сдвинули: номер недействителен, пересобрать колонки.
            self._load()
            found = self.index.get(account_id)
            if found is None:
                return -1
            return <Py_ssize_t>found
        self._sync()
        found = self.index.get(account_id)
        if found is None:
            return -1
        return <Py_ssize_t>found

    cdef Py_ssize_t _known(self, object account_id) except -2:
        cdef Py_ssize_t slot = self._slot(account_id)
        if slot < 0:
            raise KeyError(f'Нет счёта в CashStore: {account_id!r}')
        return slot

    # --- сверка с dict -----------------------------------------------------
    cdef object _reconcile_balance(self, Py_ssize_t slot, object account_id):
        """Принять значение dict, если оно изменилось в обход границы."""
        cdef object value = (<object>self.accounts[account_id])['balance']
        if <object>self.balances[slot] is not value:
            if (<object>self.balances[slot] != value
                    or type(<object>self.balances[slot]) is not type(value)):
                self.balances[slot] = value
        return self.balances[slot]

    cdef object _reconcile_reserved(self, Py_ssize_t slot, object account_id):
        """Принять reserved из dict, если он изменился в обход границы."""
        cdef object value = (<object>self.accounts[account_id]).get('reserved', 0)
        if <object>self.reserved[slot] is not value:
            if (<object>self.reserved[slot] != value
                    or type(<object>self.reserved[slot]) is not type(value)):
                self.reserved[slot] = value
        return self.reserved[slot]

    # --- balance -----------------------------------------------------------
    def get_balance(self, account_id):
        """Баланс счёта из колонки."""
        cdef Py_ssize_t slot = self._known(account_id)
        return self._reconcile_balance(slot, account_id)

    def set_balance(self, account_id, value):
        """Записать баланс: колонка, затем зеркало в dict."""
        cdef Py_ssize_t slot = self._known(account_id)
        self.balances[slot] = value
        (<object>self.accounts[account_id])['balance'] = value
        return value

    def add_balance(self, account_id, value):
        """Прибавить к балансу: та же арифметика ``balance + value``."""
        cdef Py_ssize_t slot = self._known(account_id)
        cdef object total = self._reconcile_balance(slot, account_id)+value
        self.balances[slot] = total
        (<object>self.accounts[account_id])['balance'] = total
        return total

    def move_balance(self, sender, recipient, amount):
        """Перевести сумму между счетами двумя точными вычитаниями/сложениями.

        Это не новая арифметика: прежний код делал ``sender['balance'] -=
        amount`` и ``recipient['balance'] += amount``. Один вызов нужен лишь
        для того, чтобы не платить за границу дважды.

        Перевод счёта самому себе — не редкость (возврат капитала, дивиденды
        владельцу-предприятию). Там прежний код применял обе операции к одному
        словарю, поэтому вторая считается от результата первой: ``(x-amount)
        +amount``. Это не всегда побитово равно ``x``, и подмена порядка
        сдвинула бы последний разряд.
        """
        cdef Py_ssize_t source = self._known(sender)
        cdef Py_ssize_t target = self._known(recipient)
        cdef object left = self._reconcile_balance(source, sender)-amount
        cdef object right
        self.balances[source] = left
        (<object>self.accounts[sender])['balance'] = left
        if target == source:
            right = left+amount
        else:
            right = self._reconcile_balance(target, recipient)+amount
        self.balances[target] = right
        (<object>self.accounts[recipient])['balance'] = right
        return left, right

    # --- reserved cash -----------------------------------------------------
    def get_reserved_cash(self, account_id):
        """Резерв денег счёта из колонки."""
        cdef Py_ssize_t slot = self._known(account_id)
        return self._reconcile_reserved(slot, account_id)

    def set_reserved_cash(self, account_id, value):
        """Записать резерв: колонка, затем зеркало в dict."""
        cdef Py_ssize_t slot = self._known(account_id)
        self.reserved[slot] = value
        (<object>self.accounts[account_id])['reserved'] = value
        return value

    def add_reserved_cash(self, account_id, value):
        """Прибавить к резерву: ``reserved + value`` без смены типа."""
        cdef Py_ssize_t slot = self._known(account_id)
        cdef object total = self._reconcile_reserved(slot, account_id)+value
        self.reserved[slot] = total
        (<object>self.accounts[account_id])['reserved'] = total
        return total

    def release_reserved_cash(self, account_id, value):
        """Освободить часть резерва: ``max(0, reserved - value)``.

        Порядок тот же, что в прежнем коде: сначала вычитание, затем отсечение
        нулём. Считать ``max(0, ...)`` внутри хранилища нужно, чтобы не
        заводить второй расчёт резерва в Python.
        """
        cdef Py_ssize_t slot = self._known(account_id)
        cdef object left = self._reconcile_reserved(slot, account_id)-value
        # Ровно семантика прежнего ``max(0, reserved-value)``: при неположительном
        # остатке возвращается целый 0, а не 0.0, — тип сохраняется.
        if not left > 0:
            left = 0
        self.reserved[slot] = left
        (<object>self.accounts[account_id])['reserved'] = left
        return left

    # --- обслуживание ------------------------------------------------------
    def sync(self):
        """Выровнять колонки по словарю счетов."""
        self._sync()
        self._verify()

    def columns(self):
        """Снимок колонок для тестов и диагностики (без записи в хранилище)."""
        self._sync()
        self._verify()
        return dict(keys=list(self.keys), balance=list(self.balances),
                    reserved=list(self.reserved))

    # --- копии -------------------------------------------------------------
    def __deepcopy__(self, memo):
        """Копия движка: колонки пересобираются из скопированного словаря.

        Через ``memo`` словарь счетов остаётся тем же объектом, что и
        ``engine.accounts`` копии, поэтому у копии одно хранилище.
        """
        return CashStore(copy.deepcopy(self.accounts, memo))

    def __reduce__(self):
        """Pickle: восстановить хранилище над словарём счетов."""
        return (CashStore, (self.accounts,))
