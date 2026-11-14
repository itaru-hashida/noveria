"""Граница WorldState: одна привязка реализации на импорт.

Модуль не хранит состояние движка. ``WorldState`` держит ссылки на уже
существующие хранилища (``OrderStore``, ``CashStore`` и native update state),
поэтому копий заявок, счетов и складов здесь нет.

Эталонная Python-реализация живёт в этом же модуле: она нужна как образец для
focused-теста и как fallback, если расширение не собрано. Разница только в
том, что эталон не может объявить ``cdef``-поля, поэтому тождество контейнеров
он сверяет теми же атрибутами, что и native-класс.

Модуль не импортирует ``modules.economy.support``: у границы нет EPS и других
констант проекта, поэтому циклического импорта нет.
"""

import importlib


class WorldStatePython:
    """Эталон контейнера: те же ссылки и та же сверка раскладки.

    Хранилища не копируются и не пересобираются: ``sync`` сравнивает тождество
    ``report['orders']`` и ``accounts``. Значения игры эталон тоже не меняет.

    Lifecycle ``begin_step``/``end_step`` повторяет native-ветку один в один:
    открыть шаг, привязать живые хранилища, закрыть шаг и сохранить сводку.
    """

    def __init__(self):
        self.orders = None
        self.cash = None
        self.native = None
        self._orders_source = None
        self._accounts_source = None
        self.version = 0
        self.active = False
        self.step_index = 0
        self.completed_steps = 0
        self.aborted_steps = 0
        self.diagnostics = None

    @classmethod
    def from_economy(cls, economy):
        """Построить контейнер по текущему состоянию Economy (без копирования)."""
        state = cls()
        state.sync(economy)
        return state

    def sync(self, economy):
        """Перепривязать изменившиеся части; неизменившиеся переиспользовать."""
        orders_source = economy.report['orders']
        accounts_source = economy.accounts
        changed = False
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

    def begin_step(self, economy):
        """Открыть native-шаг: привязать существующие хранилища и сверить их.

        Мир не копируется, значения заявок и денег не меняются: метод лишь
        берёт ссылки на живые хранилища движка. Повторный вызов без
        ``end_step`` — ошибка вызывающего кода.
        """
        if self.active:
            raise RuntimeError('begin_step вызван повторно без end_step')
        self.active = True
        self.step_index += 1
        self.sync(economy)
        self._verify(economy, 'begin_step')
        return self

    def end_step(self, economy, completed=True):
        """Закрыть native-шаг: сверить ссылки и сохранить диагностику.

        ``completed=False`` — шаг прерван исключением. Контейнер и тогда
        остаётся рабочим: флаг снимается, ссылки сверяются с текущими
        контейнерами движка, прерванный шаг считается отдельно. Отката
        значений нет: это не владелец данных.
        """
        if not self.active:
            raise RuntimeError('end_step вызван без begin_step')
        drifted = self._drifted(economy)
        self.active = False
        if drifted:
            self.sync(economy)
        self._verify(economy, 'end_step')
        if completed:
            self.completed_steps += 1
        else:
            self.aborted_steps += 1
        self.diagnostics = {
            'step': int(self.step_index),
            'completed': bool(completed),
            'version': int(self.version),
            'orders': 0 if self.orders is None else len(self.orders.orders),
            'accounts': 0 if self.cash is None else len(self.cash.accounts),
            'native_bound': self.native is not None,
            'rebound': bool(drifted),
            'completed_steps': int(self.completed_steps),
            'aborted_steps': int(self.aborted_steps),
        }
        return self

    def _drifted(self, economy):
        """Разошлись ли ссылки контейнера с текущими контейнерами движка."""
        if self.orders is None or self.orders.orders is not economy.report['orders']:
            return True
        if self.cash is None or self.cash.accounts is not economy.accounts:
            return True
        if self.native is not economy.__dict__.get('_native_update_state'):
            return True
        # Движок мог подменить собственный кэш границы, не меняя контейнер.
        return (economy.__dict__.get('_order_store') not in (None, self.orders)
                or economy.__dict__.get('_cash_store') not in (None, self.cash))

    def _verify(self, economy, where):
        """Проверить, что контейнер смотрит ровно на живые хранилища движка."""
        if self.orders is not None and self.orders.orders is not economy.report['orders']:
            raise RuntimeError(where+': OrderStore не связан с report["orders"]')
        if self.cash is not None and self.cash.accounts is not economy.accounts:
            raise RuntimeError(where+': CashStore не связан с accounts')
        if self.orders is None or self.cash is None:
            raise RuntimeError(where+': хранилища не привязаны')

    def reset(self):
        """Забыть привязки, не трогая значения игры; открытый шаг снимается."""
        self.active = False
        self.orders = None
        self.cash = None
        self.native = None
        self._orders_source = None
        self._accounts_source = None
        self.version += 1
        return self

    def snapshot_metadata(self):
        """Иммутабельная сводка раскладки: без выдачи живых внутренних объектов."""
        orders = self.orders
        cash = self.cash
        native = self.native
        return {
            'version': int(self.version),
            'active': bool(self.active),
            'step_index': int(self.step_index),
            'completed_steps': int(self.completed_steps),
            'aborted_steps': int(self.aborted_steps),
            'diagnostics': None if self.diagnostics is None else dict(self.diagnostics),
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
        """Независимый пустой контейнер: копия привяжется к своим контейнерам."""
        return type(self)()

    def __reduce__(self):
        """Pickle: восстановить пустой контейнер и привязать его при обращении."""
        return (type(self), ())


def _frozen_layout(layout):
    """Копия раскладки native state в неизменяемые кортежи (или None)."""
    if layout is None:
        return None
    if isinstance(layout, tuple):
        return tuple(tuple(item) if isinstance(item, (tuple, list)) else item
                     for item in layout)
    return repr(layout)


def load_world_state():
    """Вернуть native-модуль world_state или None, если он не собран.

    Отсутствие расширения даёт ImportError, а не ModuleNotFoundError: пакет
    ``modules.economy.native`` существует, а подмодуля нет. Обе ситуации
    означают одно — расширение не собрано, берём эталон.
    """
    try:
        return importlib.import_module('.world_state', __package__)
    except (ImportError, ModuleNotFoundError):
        return None


NATIVE_WORLD_STATE = False

_state = load_world_state()
if _state is not None:
    WorldState = _state.WorldState
    NATIVE_WORLD_STATE = True
else:
    WorldState = WorldStatePython
