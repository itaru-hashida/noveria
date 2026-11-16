"""Focused-тесты владения полями заявок: прямых писателей мимо OrderStore нет.

Задача — ownership, а не алгоритм. ``remaining``/``reserved``/``status``/``price``
заявки обязаны меняться только через ``OrderStore``, а Python-dict заявки
остаётся compatibility view. Проверяется это тремя независимыми способами:

* инвариант «колонка == dict» на каждом входе в границу: прямая запись в dict
  оставила бы колонку устаревшей и была бы видна сразу, а не только в конце
  месяца;
* журнал границы: каждое изменившееся поле заявки обязано иметь свой ``set_*``
  ровно в том порядке, в котором его вычислил вызывающий код;
* native и Python-эталон получают одинаковую последовательность операций и
  одинаковые колонки, включая тип int/float.

Порядок сделок, tie-break, RNG и арифметика здесь заново не проверяются: за них
отвечают match/trade/order-columns тесты, и это изменение их не трогает.
"""
import copy
import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

from modules.economy import Economy  # noqa: E402
from modules.economy import support  # noqa: E402
from modules.economy.native import order_boundary  # noqa: E402
from test_economy_native_order_columns import build_engine  # noqa: E402

EPS = support.EPS
KERNEL = order_boundary.load_order_store()

pytestmark = pytest.mark.skipif(KERNEL is None,
                                reason='Cython-расширение order_store не собрано')

order_boundary.configure(EPS)

FIELDS = ('remaining', 'reserved', 'status', 'price')
SETTERS = {'remaining': 'set_remaining', 'reserved': 'set_reserved',
           'status': 'set_status', 'price': 'set_price'}


# --- инструментированное хранилище ---------------------------------------
def journal_store(base, journal, written=None):
    """Подкласс границы: пишет след операций в ``journal``.

    ``journal`` — список троек ``(операция, id заявки, значение)``. Форма
    нарочно простая: тесты сравнивают не тексты, а порядок и число операций.
    ``written`` (если задан) получает сами объекты, в которые писала граница:
    так видно, что replay трогает копии, а не живые заявки.
    """
    def note(order, operation, value):
        if written is not None:
            written.append(order)

    def identity(order):
        # Replay пишет в диагностические копии оффера: у них нет ``id`` заявки,
        # но есть ключ ``sell_order``.
        return order.get('id', order.get('sell_order'))

    class JournalStore(base):
        def append(self, order):
            journal.append(('append', identity(order),
                            {field: order.get(field) for field in FIELDS}))
            return base.append(self, order)

        def set_remaining(self, order, value):
            note(order, 'set_remaining', value)
            journal.append(('set_remaining', identity(order), value))
            return base.set_remaining(self, order, value)

        def set_reserved(self, order, value):
            note(order, 'set_reserved', value)
            journal.append(('set_reserved', identity(order), value))
            return base.set_reserved(self, order, value)

        def set_status(self, order, value):
            note(order, 'set_status', value)
            journal.append(('set_status', identity(order), value))
            return base.set_status(self, order, value)

        def set_price(self, order, value):
            note(order, 'set_price', value)
            journal.append(('set_price', identity(order), value))
            return base.set_price(self, order, value)

    return JournalStore


def audited_store(base, divergences):
    """Подкласс границы: перед каждой операцией сверяет колонки с dict.

    Расхождение означает прямую запись в обход границы: колонка перестала быть
    источником истины. Проверка идёт до делегирования, поэтому подтягивание
    колонок внутри самой границы её не маскирует.
    """
    class AuditedStore(base):
        def _audit(self, where):
            total = len(self.orders)
            if len(self.remaining) != total:
                # Внешний append: колонки ещё не догнали список, это не мутация.
                return
            for slot, order in enumerate(self.orders):
                for field in FIELDS:
                    column = getattr(self, field)[slot]
                    value = order.get(field)
                    if column is value:
                        continue
                    if column == value and type(column) is type(value):
                        continue
                    divergences.append((where, order.get('id'), field,
                                        repr(column), repr(value)))

        def append(self, order):
            self._audit('before append')
            return base.append(self, order)

        def remove(self, order):
            self._audit('before remove')
            return base.remove(self, order)

        def set_remaining(self, order, value):
            self._audit('before set_remaining')
            return base.set_remaining(self, order, value)

        def set_reserved(self, order, value):
            self._audit('before set_reserved')
            return base.set_reserved(self, order, value)

        def set_status(self, order, value):
            self._audit('before set_status')
            return base.set_status(self, order, value)

        def set_price(self, order, value):
            self._audit('before set_price')
            return base.set_price(self, order, value)

        def slots(self, orders):
            self._audit('before slots')
            return base.slots(self, orders)

        def is_live(self, order, eps=EPS):
            self._audit('before is_live')
            return base.is_live(self, order, eps)

        def iter_live(self):
            self._audit('before iter_live')
            return base.iter_live(self)

        def sync(self):
            self._audit('before sync')
            return base.sync(self)

        def columns(self):
            self._audit('before columns')
            return base.columns(self)

    return AuditedStore


def rebind(engine, monkeypatch, cls):
    """Подменить класс границы и заставить движок взять новое хранилище."""
    monkeypatch.setattr(order_boundary, 'OrderStore', cls)
    engine.__dict__.pop('_order_store', None)
    return engine.order_store


def snapshot(engine):
    """Текущие значения четырёх полей по id заявки."""
    return {order['id']: {field: order.get(field) for field in FIELDS}
            for order in engine.report['orders']}


def assert_changes_journaled(before, after, journal):
    """Каждое изменившееся поле обязано иметь ровно один ``set_*``.

    Проверяется и значение: последняя запись журнала по этому полю совпадает с
    итогом. Так ловится и обход границы, и запись «не тем» значением.
    """
    last = {}
    for operation, identity, value in journal:
        if operation == 'append':
            continue
        field = operation.split('set_', 1)[1]
        last[(identity, field)] = value
    for identity, fields in after.items():
        old = before.get(identity)
        if old is None:
            continue
        for field in FIELDS:
            if old[field] == fields[field] and type(old[field]) is type(fields[field]):
                continue
            assert (identity, field) in last, (identity, field, 'запись мимо OrderStore')
            assert last[(identity, field)] == fields[field], (identity, field)


def one_month(engine, **options):
    """Один ход: на входе движок уже прошёл bootstrap и первый месяц."""
    engine.step()
    return engine.step(**options)


# --- 1. Колонка остаётся источником истины на каждом входе в границу ------
@pytest.mark.parametrize('planning_mode,native_mode', [
    ('v3', 'off'),
    ('v3', 'cython'),
    ('lagged_batch', 'cython'),
])
@pytest.mark.parametrize('seed', [17, 73129])
def test_columns_and_dict_agree_at_every_boundary_entry(monkeypatch, planning_mode,
                                                        native_mode, seed):
    """Прямая запись в dict сразу развела бы колонку и заявку.

    Сверка идёт перед каждой операцией границы, поэтому подтягивание колонок
    внутри ``slots``/``_sync`` не может скрыть обход.
    """
    for base in (order_boundary.OrderStore, order_boundary.OrderStorePython):
        divergences = []
        engine = build_engine(seed=seed)
        rebind(engine, monkeypatch, audited_store(base, divergences))
        engine.step()
        divergences.clear()
        engine.step(planning_mode=planning_mode, native_mode=native_mode)
        store = engine.order_store
        store.sync()
        for slot, order in enumerate(engine.report['orders']):
            for field in FIELDS:
                column = getattr(store, field)[slot]
                assert column is order.get(field) or (
                    column == order.get(field)
                    and type(column) is type(order.get(field))), (order['id'], field)
        assert divergences == [], divergences[:5]


def test_python_month_field_changes_are_journaled(monkeypatch):
    """Каждое изменение поля за месяц имеет свой ``set_*`` и своё значение."""
    journal = []
    engine = build_engine(seed=17)
    rebind(engine, monkeypatch, journal_store(order_boundary.OrderStore, journal))
    engine.step()
    before = snapshot(engine)
    journal.clear()
    engine.step()
    assert_changes_journaled(before, snapshot(engine), journal)
    assert journal, 'месяц не прошёл через границу'


def test_audit_detects_direct_dict_write(monkeypatch):
    """Контрольный опыт: детектор обязан увидеть обход, если он появится."""
    divergences = []
    engine = build_engine()
    rebind(engine, monkeypatch, audited_store(order_boundary.OrderStore, divergences))
    buy = engine.order('merchant:s', 'planks', 2.0, 'buy', 1.0, 'Товар')
    assert buy is not None
    # Обход границы: значение меняется прямо в dict.
    buy['remaining'] = 0.0
    engine.order_store.set_status(buy, 'partial')
    assert [row for row in divergences if row[2] == 'remaining'], divergences


# --- 2. Ровно одна операция границы на действие ---------------------------
def test_order_appends_through_store_once(monkeypatch):
    journal = []
    engine = build_engine()
    rebind(engine, monkeypatch, journal_store(order_boundary.OrderStore, journal))
    journal.clear()
    buy = engine.order('merchant:s', 'planks', 2.0, 'buy', 1.5, 'Товар')
    appends = [row for row in journal if row[0] == 'append']
    assert len(appends) == 1
    assert appends[0][1] == buy['id']
    assert appends[0][2]['remaining'] == 2.0
    assert engine.order_store.columns()['remaining'][-1] == 2.0


def test_cancel_writes_reserved_and_status_once(monkeypatch):
    journal = []
    engine = build_engine()
    rebind(engine, monkeypatch, journal_store(order_boundary.OrderStore, journal))
    buy = engine.order('merchant:s', 'planks', 2.0, 'buy', 1.5, 'Товар')
    journal.clear()
    engine.cancel_order(buy)
    operations = [(op, value) for op, _, value in journal]
    assert operations == [('set_reserved', 0.0), ('set_status', 'cancelled')]
    assert buy['reserved'] == 0.0 and buy['status'] == 'cancelled'
    # Отмена уже отменённой заявки не пишет ничего.
    journal.clear()
    engine.cancel_order(buy)
    assert journal == []


def test_reprice_writes_reserved_and_price_once(monkeypatch):
    journal = []
    engine = build_engine()
    rebind(engine, monkeypatch, journal_store(order_boundary.OrderStore, journal))
    engine.meta['month'] = 1
    buy = dict(id='b-reprice', participant='firm-00008', good='planks',
               quantity=10.0, remaining=10.0, price=1.0, side='buy',
               purpose='Сырьё', status='active', reason='', expires=1,
               final_consumption=False, state_id='s', province_id='p',
               tile_id='t', reserved=10.0, backing=10.0)
    engine.report['orders'] = [buy]
    engine.report['trades'] = []
    engine.report['flows'] = []
    engine.reset_report()
    engine.order_store.append(buy)
    engine.cash_store.add_reserved_cash(buy['participant'], 10.0)
    monkeypatch.setattr(engine, 'input_bid_ceiling', lambda order: 5.0)
    monkeypatch.setattr(engine, 'quoted_cost', lambda *args, **kwargs: (0.0, 2.0, True))
    journal.clear()
    engine.reprice_input_order(buy, {})
    operations = [(op, value) for op, _, value in journal]
    assert operations == [('set_reserved', 20.0), ('set_price', 2.0)]
    assert buy['reserved'] == 20.0 and buy['price'] == 2.0
    assert buy['backing'] == buy['reserved']
    assert type(buy['price']) is float


def test_trade_writes_both_orders_through_store(monkeypatch):
    """Исполнение сделки меняет остатки обеих заявок только через границу."""
    try:
        from test_economy_native_trade import commit, trade_engine
    except Exception:  # pragma: no cover - расширение trade_kernel не собрано
        pytest.skip('trade_kernel не собран')
    journal = []
    base = order_boundary.OrderStore
    engine, buy, sell, quote = trade_engine(quantity=10.0, price=5.0)
    rebind(engine, monkeypatch, journal_store(base, journal))
    before = snapshot(engine)
    journal.clear()
    commit(engine, buy, sell, quote, 4.0, 5.0)
    assert_changes_journaled(before, snapshot(engine), journal)
    operations = {(op, identity) for op, identity, _ in journal}
    assert ('set_remaining', buy['id']) in operations
    assert ('set_remaining', sell['id']) in operations


def test_replay_writes_only_diagnostic_rows(monkeypatch):
    """Replay пишет в копию оффера: живые заявки хранилища не меняются."""
    journal, written = [], []
    engine = build_engine(seed=17, detail='deep')
    engine.step()
    boards = engine.report['market_boards']
    if not boards:
        pytest.skip('книга не записана: детализация отчёта не дала board')
    board_id = boards[0]['board_id']
    store = rebind(engine, monkeypatch,
                   journal_store(order_boundary.OrderStore, journal, written))
    columns_before = store.columns()
    journal.clear()
    written.clear()
    rows = engine.replay_market_board(board_id)
    assert rows, 'replay не вернул строк'
    assert journal, 'replay не прошёл через границу'
    # Ни одна живая заявка не тронута: строки — диагностические копии.
    live = {id(order) for order in engine.report['orders']}
    assert all(id(order) not in live for order in written)
    assert store.columns() == columns_before
    for row in rows:
        assert row['status'] in ('active', 'partial', 'filled')


# --- 3. Compatibility view ------------------------------------------------
def test_setter_mirrors_value_into_dict(monkeypatch):
    journal = []
    engine = build_engine()
    store = rebind(engine, monkeypatch, journal_store(order_boundary.OrderStore, journal))
    buy = engine.order('merchant:s', 'planks', 3.0, 'buy', 1.5, 'Товар')
    store.set_remaining(buy, 2.0)
    store.set_reserved(buy, 3.0)
    store.set_status(buy, 'partial')
    store.set_price(buy, 2.5)
    columns = store.columns()
    position = store.slot(buy)
    assert columns['remaining'][position] == buy['remaining'] == 2.0
    assert columns['reserved'][position] == buy['reserved'] == 3.0
    assert columns['status'][position] == buy['status'] == 'partial'
    assert columns['price'][position] == buy['price'] == 2.5


def test_direct_dict_write_creates_no_second_state(monkeypatch):
    """Запись в dict подхватывается колонкой и не живёт отдельно."""
    journal = []
    engine = build_engine()
    store = rebind(engine, monkeypatch, journal_store(order_boundary.OrderStore, journal))
    buy = engine.order('merchant:s', 'planks', 3.0, 'buy', 1.5, 'Товар')
    buy['remaining'] = 1.0
    store.slots([buy])
    assert store.columns()['remaining'][store.slot(buy)] == 1.0
    store.set_remaining(buy, 0.5)
    assert buy['remaining'] == 0.5
    assert store.columns()['remaining'][store.slot(buy)] == 0.5


def test_int_and_float_types_are_preserved(monkeypatch):
    journal = []
    engine = build_engine()
    store = rebind(engine, monkeypatch, journal_store(order_boundary.OrderStore, journal))
    buy = engine.order('merchant:s', 'planks', 2, 'buy', 1, 'Товар')
    assert type(buy['remaining']) is int
    assert type(buy['price']) is int
    store.set_remaining(buy, 0.1 + 0.2)
    store.set_price(buy, 2)
    assert type(store.columns()['remaining'][store.slot(buy)]) is float
    assert store.columns()['remaining'][store.slot(buy)].hex() == (0.1 + 0.2).hex()
    assert type(buy['price']) is int


def test_exhausted_offer_does_not_resurrect(monkeypatch):
    """Исчерпание живёт в колонке: запись в dict его не отменяет."""
    journal = []
    engine = build_engine()
    store = rebind(engine, monkeypatch, journal_store(order_boundary.OrderStore, journal))
    buy = engine.order('merchant:s', 'planks', 3.0, 'buy', 1.5, 'Товар')
    store.set_remaining(buy, 0.0)
    assert store.is_live(buy, EPS) is False
    buy['remaining'] = 5.0            # попытка «воскресить» через dict
    assert store.is_live(buy, EPS) is False
    store.set_status(buy, 'cancelled')
    store.set_remaining(buy, 5.0)     # остаток вернули, но заявка отменена
    assert store.is_live(buy, EPS) is False


# --- 4. Слоты, append и защита от устаревшего слота ----------------------
def test_append_and_slot_assignment(monkeypatch):
    journal = []
    engine = build_engine()
    store = rebind(engine, monkeypatch, journal_store(order_boundary.OrderStore, journal))
    buy = engine.order('merchant:s', 'planks', 2.0, 'buy', 1.5, 'Товар')
    sell = engine.order('merchant:s', 'planks', 1.0, 'sell', 2.0, 'Продажа')
    assert store.slot(buy) == 0
    assert store.slot(sell) == 1
    store.remove(buy)
    assert store.slot(sell) == 0
    assert store.slot(buy) == -1
    assert len(store.columns()['remaining']) == len(engine.report['orders'])


def test_stale_slot_is_rejected_by_identity(monkeypatch):
    journal = []
    engine = build_engine()
    store = rebind(engine, monkeypatch, journal_store(order_boundary.OrderStore, journal))
    buy = engine.order('merchant:s', 'planks', 2.0, 'buy', 1.5, 'Товар')
    # Вторая заявка уводит memo хранилища с первой: путь идёт через индекс.
    engine.order('merchant:s', 'planks', 1.0, 'buy', 1.5, 'Товар')
    stale = store.index[id(buy)]
    # Имитируем переиспользование id: заявка исчезла из списка, а слот в
    # индексе остался и указывает на чужой объект.
    engine.report['orders'][stale] = dict(
        id='impostor', participant='merchant:s', good='planks', quantity=1.0,
        remaining=1.0, price=1.0, status='active', reserved=0.0, side='buy')
    store.set_remaining(buy, 0.0)
    # Колонка не тронута: слот не подтвердился тождеством, запись ушла в dict.
    assert store.columns()['remaining'][stale] == 2.0
    assert buy['remaining'] == 0.0
    assert id(buy) not in store.index


def test_unknown_order_gets_dict_only_write(monkeypatch):
    journal = []
    engine = build_engine()
    store = rebind(engine, monkeypatch, journal_store(order_boundary.OrderStore, journal))
    ghost = dict(id='ghost', participant='merchant:s', good='planks',
                 quantity=1.0, remaining=1.0, price=1.0, status='active',
                 reserved=0.0, side='buy')
    before = store.columns()
    store.set_remaining(ghost, 0.0)
    assert ghost['remaining'] == 0.0
    assert store.columns() == before


# --- 5. Копия и fallback --------------------------------------------------
def test_deepcopy_does_not_share_store(monkeypatch):
    journal = []
    engine = build_engine()
    rebind(engine, monkeypatch, journal_store(order_boundary.OrderStore, journal))
    engine.step()
    clone = copy.deepcopy(engine)
    assert clone.order_store is not engine.order_store
    assert clone.order_store.orders is clone.report['orders']
    target = clone.report['orders'][0]
    clone.order_store.set_remaining(target, 0.0)
    assert target['remaining'] == 0.0
    assert engine.order_store.columns()['remaining'][0] != 0.0 or \
        engine.report['orders'][0]['remaining'] != 0.0


def test_python_fallback_keeps_same_invariant(monkeypatch):
    """Без .pyd эталон держит тот же инвариант и тот же месяц."""
    monkeypatch.setattr(order_boundary, 'OrderStore', order_boundary.OrderStorePython)
    engine = build_engine(seed=17)
    engine.__dict__.pop('_order_store', None)
    engine.step()
    engine.step(native_mode='cython')
    store = engine.order_store
    for slot, order in enumerate(engine.report['orders']):
        for field in FIELDS:
            assert getattr(store, field)[slot] == order.get(field)


def test_load_returns_none_when_extension_absent(monkeypatch):
    real_import = importlib.import_module

    def fake_import(name, package=None):
        if name.endswith('order_store'):
            raise ImportError("cannot import name 'order_store'")
        return real_import(name, package)

    monkeypatch.setattr(importlib, 'import_module', fake_import)
    assert order_boundary.load_order_store() is None


# --- 6. Differential native vs Python reference ---------------------------
def test_native_and_reference_receive_same_operation_sequence(monkeypatch):
    """Одна и та же последовательность операций даёт одинаковые колонки."""
    def run(base):
        journal = []
        engine = build_engine(seed=17)
        rebind(engine, monkeypatch, journal_store(base, journal))
        engine.step()
        return journal, engine

    native_journal, native_engine = run(order_boundary.OrderStore)
    monkeypatch.undo()
    reference_journal, reference_engine = run(order_boundary.OrderStorePython)
    assert native_journal == reference_journal
    native_columns = native_engine.order_store.columns()
    reference_columns = reference_engine.order_store.columns()
    assert native_columns == reference_columns
    for field in FIELDS:
        assert [type(value) for value in native_columns[field]] == \
               [type(value) for value in reference_columns[field]]


# --- 7. production_price: тип берётся из живого источника ----------------
@pytest.mark.parametrize('seed', [17, 73129])
def test_recovery_production_price_keeps_int_type(seed):
    """``production_price`` обязан совпасть с эталоном и по значению, и по типу.

    V3 растит цену восстановления из живого ``price`` предприятия, поэтому int
    остаётся int: 1 и 1.0 семантически различаются и влияют на встроенный sum().
    Проверяется именно пара «значение + тип», а не «круглость» числа.
    """
    def run(mode):
        engine = build_engine(seed=seed)
        for _ in range(3):
            engine.step(native_mode=mode)
        return {e['id']: e.get('production_price')
                for e in engine.enterprises.values()}

    native = run('cython')
    reference = run('off')
    assert native == reference
    assert {key: type(value) for key, value in native.items()} == \
           {key: type(value) for key, value in reference.items()}
    # Мир обязан содержать int-цену, иначе тест не проверяет сохранение типа.
    assert any(type(value) is int for value in reference.values())


def test_reference_store_has_same_surface():
    for name in ('append', 'remove', 'set_remaining', 'set_reserved',
                 'set_status', 'set_price', 'is_live', 'sync', 'columns',
                 'slots', 'iter_live', 'slot'):
        assert callable(getattr(order_boundary.OrderStorePython, name))
        assert callable(getattr(order_boundary.OrderStore, name))
