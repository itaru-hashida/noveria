# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Cython-owned денежные поля счетов: колонки — истина, dict — compatibility view.

Хранилище импортирует живой словарь ``engine.accounts`` и держит ``balance``,
``reserved`` и ``protected`` в параллельных колонках. Дальше истина живёт в
колонках: каждая операция пишет колонку, а затем повторяет значение в
Python-dict, чтобы весь остальной код продолжал читать счёт как обычный
словарь. Второго независимо изменяемого состояния нет: dict — это отражение
колонок.

Колонки — списки Python-объектов, а не типизированные массивы. Это требование
точности: ``balance``/``reserved``/``protected`` бывают и ``int``, и ``float``,
а приведение к ``double`` поменяло бы тип и последний разряд, от которого
зависят порядок сделок и RNG. Значение кладётся в колонку как есть, арифметика
выполняется обычным Python-сложением над ``object``, поэтому порядок операций
тот же, что и в прежнем коде: ``balance - amount``, ``reserved + extra``.

``protected`` — отдельное понятие, а не второй ``reserved``: резерв под заявки
и защита кассы под обязательства (проект, зарплаты, доставка) считаются
независимо и не сводятся друг к другу. Складываются они только в проверках
инварианта и в единой формуле свободной кассы, где вычитаются по очереди.

Словарь счетов может вырасти в обход границы: ``open_enterprise`` и
``sync_locations`` создают счета прямо в ``engine.accounts``. Поэтому любая
операция сначала выравнивает хвост колонок по словарю: новый счёт
импортируется из своего словаря. Это проверка длин, а не скан.

Обратная ситуация — прямая запись в dict в обход границы. Так делают
native-ветки восстановления предприятий: они меняют ``balance``/``protected``
напрямую. Чтобы не завести второе независимое состояние, каждая мутация и
чтение сверяют колонку с dict и, если dict новее, принимают его значение.

Приём значения из dict — это compatibility-режим, и он явный: ``adopt_mismatch``
хранит, сколько раз колонка догнала разошедшийся dict, а ``strict`` запрещает
такое принятие ошибкой. Обычный путь мутации (``set_*``/``add_*``/
``move_balance``) никогда не импортирует произвольное значение: он пишет и
колонку, и dict. Импорт из dict — только у ``_append`` (новый счёт или
пересборка раскладки), ``adopt`` (явный импорт) и сверки чтения.
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
    cdef public list protected
    cdef public dict index
    cdef public unsigned long version
    # Compatibility-режим: сколько раз колонка принимала разошедшийся dict.
    # Ноль означает, что обычный путь ни разу не импортировал чужое значение.
    cdef public unsigned long adopt_mismatch
    cdef public bint strict

    def __cinit__(self, object accounts, bint strict=False):
        self.accounts = accounts
        # Номер раскладки. Меняется при пересборке колонок; нужен диагностике и
        # тестам, чтобы отличить импорт от продолжения работы.
        self.version = 0
        self.adopt_mismatch = 0
        # Strict запрещает принимать значение dict в обход границы: обычный
        # путь обязан идти через ``set_*``/``add_*``. Импорт при новой
        # раскладке разрешён и в strict: без него счёт не завести.
        self.strict = strict
        self._load()

    # --- импорт текущих значений ------------------------------------------
    cdef int _load(self) except -1:
        """Считать колонки из словаря: единственный импорт значений."""
        cdef object account_id
        self.keys = []
        self.balances = []
        self.reserved = []
        self.protected = []
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
        self.protected.append(account.get('protected', 0))
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
    cdef object _adopt(self, list column, Py_ssize_t slot, object account_id,
                       object field, object default):
        """Принять значение dict, если оно изменилось в обход границы.

        Это compatibility-путь: он видит прямой writer в dict и подтягивает
        колонку. В strict такое расхождение — ошибка, а не молчаливое
        принятие: иначе два владельца снова стали бы неотличимы. Импорт при
        пересборке раскладки сюда не идёт — его делает ``_append``.
        """
        cdef object account = self.accounts[account_id]
        cdef object value
        if default is None:
            value = account[field]
        else:
            value = account.get(field, default)
        if <object>column[slot] is not value:
            if (<object>column[slot] != value
                    or type(<object>column[slot]) is not type(value)):
                if self.strict:
                    raise ValueError(
                        f'CashStore: {field} счёта {account_id!r} изменён в обход '
                        f'владельца (колонка {<object>column[slot]!r}, dict {value!r})')
                column[slot] = value
                self.adopt_mismatch += 1
        return column[slot]

    cdef object _reconcile_balance(self, Py_ssize_t slot, object account_id):
        """Принять значение dict, если оно изменилось в обход границы."""
        return self._adopt(self.balances, slot, account_id, 'balance', None)

    cdef object _reconcile_reserved(self, Py_ssize_t slot, object account_id):
        """Принять reserved из dict, если он изменился в обход границы."""
        return self._adopt(self.reserved, slot, account_id, 'reserved', 0)

    cdef object _reconcile_protected(self, Py_ssize_t slot, object account_id):
        """Принять protected из dict, если он изменился в обход границы."""
        return self._adopt(self.protected, slot, account_id, 'protected', 0)

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

    # --- protected cash ----------------------------------------------------
    def get_protected_cash(self, account_id):
        """Защита кассы счёта из колонки.

        ``protected`` — не второй ``reserved``: он держит обязательства счёта
        (проект, зарплаты, доставку), а ``reserved`` — заявки. Понятия разные,
        поэтому у них отдельные колонки и отдельные методы.
        """
        cdef Py_ssize_t slot = self._known(account_id)
        return self._reconcile_protected(slot, account_id)

    def set_protected_cash(self, account_id, value):
        """Записать защиту кассы: колонка, затем зеркало в dict."""
        cdef Py_ssize_t slot = self._known(account_id)
        self.protected[slot] = value
        (<object>self.accounts[account_id])['protected'] = value
        return value

    def add_protected_cash(self, account_id, value):
        """Прибавить к защите: ``protected + value`` без смены типа.

        Порядок прежний: ``account['protected'] += hold``. Тип результата
        складывается из типов слагаемых, поэтому колонка хранит его как есть.
        """
        cdef Py_ssize_t slot = self._known(account_id)
        cdef object total = self._reconcile_protected(slot, account_id)+value
        self.protected[slot] = total
        (<object>self.accounts[account_id])['protected'] = total
        return total

    def release_protected_cash(self, account_id, value):
        """Снять часть защиты: ``max(0, protected - value)``.

        Порядок тот же, что в прежнем коде: сначала вычитание, затем отсечение
        нулём. Возвращается целый ``0`` при неположительном остатке — ровно
        семантика встроенного ``max(0, ...)``, тип сохраняется.
        """
        cdef Py_ssize_t slot = self._known(account_id)
        cdef object left = self._reconcile_protected(slot, account_id)-value
        if not left > 0:
            left = 0
        self.protected[slot] = left
        (<object>self.accounts[account_id])['protected'] = left
        return left

    # --- свободная касса ---------------------------------------------------
    def free_cash(self, account_id):
        """Единственная формула свободной кассы: ``max(0.0, balance-reserved-protected)``.

        Порядок вычитаний прежний и обязательный: сначала ``reserved``, затем
        ``protected``; ``max`` возвращает ``0.0``, а не целый ``0``. Тот же
        метод отдаёт и читаемая функция модуля ``free_cash``, и Python-адаптер
        ``Economy.free_cash``, поэтому второй реализации формулы нет.
        """
        cdef Py_ssize_t slot = self._known(account_id)
        cdef object balance = self._reconcile_balance(slot, account_id)
        cdef object reserved = self._reconcile_reserved(slot, account_id)
        cdef object protected = self._reconcile_protected(slot, account_id)
        cdef object free = balance-reserved-protected
        if not free > 0.0:
            return 0.0
        return free

    # --- обслуживание ------------------------------------------------------
    def sync(self):
        """Выровнять колонки по словарю счетов."""
        self._sync()
        self._verify()

    def adopt(self):
        """Явный импорт значений dict во все колонки.

        Это путь load/new-account: сюда приходят значения, которые владелец не
        писал — восстановленные из checkpoint, собранные миграцией или
        выставленные тестом. Обычная мутация им не пользуется, поэтому
        произвольная правка dict не подменяет владельца молча: сверка чтения
        учитывает её отдельно и диагностируется через ``adopt_mismatch``.
        """
        cdef Py_ssize_t slot, total = len(self.keys)
        cdef object account_id
        self._sync()
        self._verify()
        for slot in range(total):
            account_id = <object>self.keys[slot]
            self.balances[slot] = (<object>self.accounts[account_id])['balance']
            self.reserved[slot] = (<object>self.accounts[account_id]).get('reserved', 0)
            self.protected[slot] = (<object>self.accounts[account_id]).get('protected', 0)
        return self

    def columns(self):
        """Снимок колонок для тестов и диагностики (без записи в хранилище)."""
        self._sync()
        self._verify()
        return dict(keys=list(self.keys), balance=list(self.balances),
                    reserved=list(self.reserved), protected=list(self.protected))

    def diagnostics(self):
        """Сводка compatibility-режима: раскладка и число принятых расхождений."""
        return dict(version=int(self.version), accounts=len(self.keys),
                    adopt_mismatch=int(self.adopt_mismatch),
                    strict=bool(self.strict))

    # --- копии -------------------------------------------------------------
    def __deepcopy__(self, memo):
        """Копия движка: колонки пересобираются из скопированного словаря.

        Через ``memo`` словарь счетов остаётся тем же объектом, что и
        ``engine.accounts`` копии, поэтому у копии одно хранилище.
        """
        return type(self)(copy.deepcopy(self.accounts, memo), self.strict)

    def __reduce__(self):
        """Pickle: восстановить хранилище над словарём счетов."""
        return (type(self), (self.accounts,))
