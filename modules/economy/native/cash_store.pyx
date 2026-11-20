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

Обратная ситуация — прямая запись в dict в обход границы. Раньше так делали
native-ветки восстановления предприятий, и хранилище молча принимало значение
dict при каждом чтении. Теперь это запрещено: ``strict`` включён по умолчанию,
и расхождение колонки с dict — ошибка ``ValueError``, а не скрытое
восстановление. Иначе у поля снова было бы два владельца, а порядок операций
зависел бы от того, кто прочитал счёт первым.

Импорт значений из dict остался ровно на границе импорта: ``_load`` (создание
хранилища над словарём счетов) и ``_relayout`` (новый счёт появился в словаре).
Обычный путь мутации (``set_*``/``add_*``/``release_*``/``move_balance``) пишет
и колонку, и dict, а запись мимо владельца падает.

Разница между импортом и обходом — это не «когда прочитали», а «идёт ли
штатный прогон». Движок открывает окно владения на время хода
(``begin_runtime``/``end_runtime``): внутри окна любое расхождение колонки с
dict — ошибка ``ValueError``, потому что писать деньги мимо владельца в
runtime нельзя. Вне окна значения dict принимаются и считаются в
``adopt_mismatch``: это граница импорта старого состояния — загрузка
checkpoint, откат хода, сборка счёта инструментом или тестом.

Граница импорта — это ровно ``begin_runtime``: он вызывает ``adopt`` и тем
самым принимает расхождения, накопленные до месяца. Дальше, до
``end_runtime``, приём запрещён. Поэтому ``adopt_mismatch`` — наблюдаемая
величина, а не догадка: в штатном прогоне она остаётся нулём, потому что все
writers идут через владельца, а ненулевое значение означает, что состояние
пришло извне границы. Именно это проверяет
``test_economy_native_cash_runtime.py`` на всём жизненном цикле движка.

``strict=False`` выключает и это правило: compatibility-режим нужен только для
явного импорта старого состояния и differential-тестов и никогда не
включается движком.

Пересборка словаря — не импорт. ``step`` пересобирает ``engine.accounts``
сортировкой, и граница перепривязывается к новому словарю. При этом те же
живые счета обязаны совпадать с колонками: ``verify`` сверяет их до создания
нового хранилища и падает на расхождении, если окно владения открыто. Окно
переносится на новое хранилище, иначе одна сортировка словаря выключала бы
строгий режим до конца месяца.
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
    # Ноль означает, что штатный прогон ни разу не импортировал чужое значение.
    cdef public unsigned long adopt_mismatch
    # Счета, по которым расхождение было принято: диагностика показывает не
    # только «сколько», но и «где», иначе обход владельца неотличим от шума.
    cdef public list adopt_accounts
    cdef public bint strict
    # Окно владения: открыто на время хода. Внутри окна расхождение колонки с
    # dict — ошибка, а не импорт. Вне окна значениe dict принимается: это
    # граница импорта старого состояния (checkpoint, откат, сборка счёта).
    cdef public bint runtime

    def __cinit__(self, object accounts, bint strict=True):
        self.accounts = accounts
        # Номер раскладки. Меняется при пересборке колонок; нужен диагностике и
        # тестам, чтобы отличить импорт от продолжения работы.
        self.version = 0
        self.adopt_mismatch = 0
        self.adopt_accounts = []
        # Strict выключает compatibility целиком: и импорт вне окна, и приём
        # расхождения. Движок его не выключает — режим только для импорта
        # старого состояния и differential-тестов.
        self.strict = strict
        # Окно владения по умолчанию закрыто: только что созданное хранилище
        # ещё не начало прогон, и его значения — это импорт словаря счетов.
        self.runtime = False
        self._load()

    def __reduce__(self):
        """Pickle хранит только авторитетный dict и режим проверки."""
        return type(self), (self.accounts, self.strict)

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
        """Сверить колонку с dict: обход владельца или законный импорт.

        Внутри окна владения (``runtime``) расхождение — ошибка: это штатный
        прогон, и деньги здесь пишет только хранилище. Вне окна расхождение —
        импорт состояния: checkpoint, откат хода, инструмент или фикстура
        теста. Такой импорт принимается и считается в ``adopt_mismatch``.

        Граница импорта держится на измерении, а не на обещании: в штатном
        жизненном цикле (старт мира, месяцы, откат, save/load, игровые
        действия) ``adopt_mismatch`` остаётся нулём — это проверяет
        ``test_economy_native_cash_runtime.py``. Ненулевое значение означает,
        что состояние пришло извне границы владельца.

        ``strict=False`` разрешает приём и внутри окна: это compatibility для
        differential-тестов и явного импорта старого состояния.
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
                if self.strict and self.runtime:
                    raise ValueError(
                        f'CashStore: {field} счёта {account_id!r} изменён в обход '
                        f'владельца (колонка {<object>column[slot]!r}, dict {value!r})')
                column[slot] = value
                self.adopt_mismatch += 1
                # Счёт, по которому границу обошли: без него счётчик не говорит,
                # какой writer писал мимо владельца.
                if account_id not in self.adopt_accounts:
                    self.adopt_accounts.append(account_id)
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

    def set_payroll_protected(self, account_id, value):
        """Записать companion-поле защиты зарплаты через owner API."""
        (<object>self.accounts[account_id])['payroll_protected'] = value
        return value

    def release_operating_protected(self, account_id):
        """Снять companion-поле операционной защиты через owner API."""
        return (<object>self.accounts[account_id]).pop('operating_protected', 0)

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

    def begin_runtime(self):
        """Открыть окно владения месяцем: явный импорт на границе.

        Месяц начинается с состояния, которое уже лежит в словаре счетов:
        checkpoint, откат хода, инструмент или тест могли выставить денежные
        поля в обход границы. Импорт делается здесь и только здесь — вызовом
        ``adopt``, а не побочным эффектом чтения. Принятые расхождения видны в
        ``adopt_mismatch``: в нормальном прогоне счётчик остаётся нулём,
        потому что все writers идут через владельца.

        После этого приём запрещён до ``end_runtime``: любое расхождение —
        ошибка, иначе появился бы второй владелец поля.
        """
        self.adopt()
        self.runtime = True
        return self

    def end_runtime(self, bint check=True):
        """Закрыть окно владения.

        Перед закрытием колонки сверяются со словарём: запись мимо владельца,
        которую никто не прочитал за месяц, видна здесь. Проверка отключена
        только для аварийного выхода, где уже есть исходное исключение.
        """
        if check:
            self.verify()
        self.runtime = False
        return self

    def verify(self):
        """Сверить колонки с живым словарём счетов и упасть на расхождении.

        Проверка идёт при открытом окне: вызывающий передаёт уже собранное
        хранилище заново, например после пересборки словаря сортировкой в
        ``step``. Это не импорт, а значит любое расхождение — обход владельца.
        """
        cdef Py_ssize_t slot, total = len(self.keys)
        cdef object account_id
        self._sync()
        self._verify()
        for slot in range(total):
            account_id = <object>self.keys[slot]
            self._reconcile_balance(slot, account_id)
            self._reconcile_reserved(slot, account_id)
            self._reconcile_protected(slot, account_id)
        return self

    def adopt(self):
        """Явный импорт значений dict во все колонки: граница импорта.

        Это путь загрузки: сюда приходят значения, которые владелец не писал —
        восстановленные из checkpoint, собранные миграцией или выставленные
        инструментом. Обычная мутация им не пользуется. Каждое принятое
        расхождение считается в ``adopt_mismatch``: ноль в обычном прогоне и
        есть доказательство, что runtime не писал cash в dict напрямую.
        """
        cdef Py_ssize_t slot, total = len(self.keys)
        cdef object account_id, account, value
        self._sync()
        self._verify()
        for slot in range(total):
            account_id = <object>self.keys[slot]
            account = self.accounts[account_id]
            value = account['balance']
            if (<object>self.balances[slot] is not value
                    and (<object>self.balances[slot] != value
                         or type(<object>self.balances[slot]) is not type(value))):
                self.balances[slot] = value
                self.adopt_mismatch += 1
                if account_id not in self.adopt_accounts:
                    self.adopt_accounts.append(account_id)
            value = account.get('reserved', 0)
            if (<object>self.reserved[slot] is not value
                    and (<object>self.reserved[slot] != value
                         or type(<object>self.reserved[slot]) is not type(value))):
                self.reserved[slot] = value
                self.adopt_mismatch += 1
                if account_id not in self.adopt_accounts:
                    self.adopt_accounts.append(account_id)
            value = account.get('protected', 0)
            if (<object>self.protected[slot] is not value
                    and (<object>self.protected[slot] != value
                         or type(<object>self.protected[slot]) is not type(value))):
                self.protected[slot] = value
                self.adopt_mismatch += 1
                if account_id not in self.adopt_accounts:
                    self.adopt_accounts.append(account_id)
        return self

    def columns(self):
        """Снимок колонок для тестов и диагностики (без записи в хранилище)."""
        self._sync()
        self._verify()
        return dict(keys=list(self.keys), balance=list(self.balances),
                    reserved=list(self.reserved), protected=list(self.protected))

    def diagnostics(self):
        """Сводка режима владения: раскладка, окно и принятые расхождения."""
        return dict(version=int(self.version), accounts=len(self.keys),
                    adopt_mismatch=int(self.adopt_mismatch),
                    adopt_accounts=list(self.adopt_accounts),
                    strict=bool(self.strict), runtime=bool(self.runtime))

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
