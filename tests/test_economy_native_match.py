"""Тесты Cython-цикла одного buyer order (``run_match``).

Python по-прежнему формирует порядок buyer orders и market book: тесты
передают ядру готовый ``OfferView``. Проверяется, что перенесённый участок
сохранил ровно прежнее поведение:

* порядок offers и курсор ``first_live`` (обход идёт через представление);
* tie-break по ID и расход RNG (``owner.shuffled``);
* EPS: сделки с ``q <= EPS`` не исполняются, цена выше лимита пропускается;
* расчёт ``q`` с хуком ``purchase_quantity``;
* остановку при полном исполнении buyer order;
* вызов существующего ``commit_trade`` и работу хуков
  ``transfer``/``use_stock``/``add_stock`` движка.

Главная проверка — побитовое совпадение native-цикла и ``run_match_python`` на
одинаковом состоянии. Тесты не собирают мир целиком: для одного buyer order
достаточно маленького движка, живой книги и двух заявок.
"""
import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.economy import Economy  # noqa: E402
from modules.economy import support  # noqa: E402
from modules.economy.native import match_boundary  # noqa: E402
from modules.economy.native import offer_boundary  # noqa: E402
from modules.economy.native import order_boundary  # noqa: E402
from modules.economy.native import trade_boundary  # noqa: E402

EPS = support.EPS
KERNEL = match_boundary.load_match_kernel()

pytestmark = pytest.mark.skipif(KERNEL is None,
                                reason='Cython-расширение match_kernel не собрано')

order_boundary.configure(EPS)
offer_boundary.configure(EPS)

GOOD = 'planks'


# --- вспомогательные функции ---------------------------------------------
def build_engine(seed=17, detail='none'):
    """Маленький движок из фикстуры: счета, фирмы, товары, конфиг."""
    cfg = json.loads((ROOT/'tests/fixtures/economy.json').read_text(encoding='utf-8'))
    entities = [dict(id='s', kind='State', name='S', properties={'capital_id': 'c'}),
        dict(id='p', kind='Province', name='P', properties={'capital_id': 'c'}),
        dict(id='c', kind='City', name='C', properties={'province_id': 'p', 'tile_id': 't'}),
        dict(id='h', kind='SocialGroup', name='H',
             properties={'owner_id': 'p', 'population': 100000})]
    links = [dict(source='s', target=i, kind='owns') for i in ('p', 'c')]
    tile = dict(id='t', x=0, y=0, passable=True, cost=1, road=True, province_id='p',
        resources=['зерно', 'лес', 'железо', 'рыба'], terrain='plain')
    engine = Economy(dict(entities=entities, links=links), [tile],
        meta=dict(id='game', version=1, month=0, config=cfg))
    engine.meta['report_detail'] = detail
    engine.seed = seed
    engine.rng.seed(seed)
    engine.bootstrap()
    return engine


def sell(identity, participant, remaining, price, quantity=None, province_id='p',
         status='active', tile_id='t'):
    """Живая sell-заявка: обычный словарь, как у настоящего движка."""
    return dict(id=identity, participant=participant, good=GOOD,
        quantity=remaining if quantity is None else quantity,
        remaining=remaining, price=price, side='sell', purpose='Продажа запасов',
        status=status, reason='', expires=1, final_consumption=False,
        state_id='s', province_id=province_id, tile_id=tile_id,
        reserved=0, backing=remaining)


def buy_order(participant, remaining, price, quantity=None, purpose='Товар'):
    """Buy-заявка с резервом, как его ставит ``Economy.order``."""
    return dict(id='b1', participant=participant, good=GOOD,
        quantity=remaining if quantity is None else quantity,
        remaining=remaining, price=price, side='buy', purpose=purpose,
        status='active', reason='', expires=1, final_consumption=False,
        state_id='s', province_id='p', tile_id='t',
        reserved=remaining*price, backing=remaining*price)


def match_scene(sellers, buyer_cash=1000.0, buy_remaining=10.0, buy_price=5.0,
                freight=0.0):
    """Движок, живая книга из ``sellers`` и одна buy-заявка.

    ``sellers`` — пары ``(id, remaining, price_продавца)``. Склад и средняя
    стоимость продавца выставляются так, чтобы сделка была исполнимой.
    """
    engine = build_engine()
    engine.meta['month'] = 1
    buyer = engine.accounts['merchant:s']
    # Провинция покупателя задаёт фильтр ``local_only`` в представлении.
    buyer['province_id'] = 'p'
    buyer['balance'] = buyer_cash
    buyer['reserved'] = 0.0
    buyer['stock'] = {}
    buyer['values'] = {}
    buy = buy_order('merchant:s', buy_remaining, buy_price)
    orders = [buy]
    for identity, remaining, price in sellers:
        participant = f'firm-{identity}'
        account = engine.accounts.setdefault(participant, dict(
            id=participant, kind='firm', state_id='s', province_id='p',
            tile_id='t', balance=100.0, reserved=0.0,
            stock={}, values={}, stock_reserved={}))
        account['stock'] = {GOOD: remaining}
        account['values'] = {GOOD: remaining*price}
        account['stock_reserved'] = {GOOD: remaining}
        orders.append(sell(identity, participant, remaining, price))
    engine.report['orders'] = orders
    engine.report['trades'] = []
    engine.report['flows'] = []
    engine.reset_report()
    for order in orders:
        engine.order_store.append(order)
    engine.cash_store.add_reserved_cash('merchant:s', buy['reserved'])
    sells = {GOOD: orders[1:]}
    options = engine.offers(buyer, GOOD, sells)
    return engine, buy, sells, options


def run(engine, buy, sells, options, runner=None):
    """Выбранная реализация цикла с теми же аргументами, что и в ``match``."""
    function = runner or match_boundary.run_match
    return function(engine, buy, options, sells, EPS,
                    engine.cash_store, engine.order_store,
                    trade_boundary.commit_trade)


def snapshot(engine, buy):
    """Состояние, которое меняет цикл: деньги, товар, заявки, сделки, RNG."""
    store = engine.order_store
    store.sync()
    return dict(
        rng=engine.rng.getstate(),
        balances={key: value['balance'] for key, value in engine.accounts.items()},
        reserved={key: value.get('reserved', 0) for key, value in engine.accounts.items()},
        stocks={key: dict(value['stock']) for key, value in engine.accounts.items()},
        values={key: dict(value['values']) for key, value in engine.accounts.items()},
        stock_reserved={key: dict(value.get('stock_reserved', {}))
                        for key, value in engine.accounts.items()},
        flows=copy.deepcopy(engine.report['flows']),
        trades=copy.deepcopy(engine.report['trades']),
        orders=[dict(order) for order in engine.report['orders']],
        columns=store.columns(),
        demand_revision=dict(engine._demand_revision),
        trades_by_seller=dict(engine._trades_by_seller),
    )


def encoded(engine, buy):
    """JSON с сортировкой ключей: сравнение без учёта порядка словарей."""
    return json.dumps(snapshot(engine, buy), sort_keys=True, default=str)


# --- 1. обычное исполнение и порядок offers -------------------------------
def test_single_offer_is_fully_executed():
    engine, buy, sells, options = match_scene([('00008', 10.0, 5.0)])
    run(engine, buy, sells, options)
    assert buy['remaining'] == 0
    assert len(engine.report['trades']) == 1
    assert engine.accounts['merchant:s']['stock'][GOOD] == 10.0


def test_offers_are_consumed_in_book_order():
    """Порядок offers задаёт книга, а не порядок заявок в списке."""
    engine, buy, sells, options = match_scene(
        [('00009', 10.0, 4.5), ('00008', 10.0, 4.0)], buy_remaining=20.0)
    run(engine, buy, sells, options)
    # Книга отсортирована по полной цене: сначала дешёвый оффер.
    assert [trade['sell_order'] for trade in engine.report['trades']] == \
           ['00008', '00009']


def test_loop_stops_as_soon_as_buy_is_filled():
    engine, buy, sells, options = match_scene(
        [('00008', 10.0, 1.0), ('00009', 10.0, 2.0), ('00010', 10.0, 3.0)],
        buy_remaining=4.0)
    run(engine, buy, sells, options)
    # Полное исполнение на первом оффере: остальные не тронуты.
    assert buy['remaining'] == 0
    assert len(engine.report['trades']) == 1
    assert engine.report['orders'][2]['remaining'] == 10.0
    assert engine.report['orders'][3]['remaining'] == 10.0


def test_partial_fill_continues_to_next_offer():
    engine, buy, sells, options = match_scene(
        [('00008', 4.0, 1.0), ('00009', 10.0, 2.0)], buy_remaining=10.0)
    run(engine, buy, sells, options)
    assert buy['remaining'] == 0
    assert [trade['quantity'] for trade in engine.report['trades']] == [4.0, 6.0]


# --- 2. цена, EPS и q -----------------------------------------------------
def test_offer_above_buy_limit_is_skipped():
    engine, buy, sells, options = match_scene(
        [('00008', 10.0, 9.0), ('00009', 10.0, 4.0)], buy_price=5.0)
    run(engine, buy, sells, options)
    assert [trade['sell_order'] for trade in engine.report['trades']] == ['00009']
    # Дорогой оффер остался живым: цикл его лишь пропустил.
    assert engine.report['orders'][1]['remaining'] == 10.0


def test_quantity_is_zero_when_reserve_is_exhausted():
    """``q`` учитывает резерв заявки: без денег сделки не будет."""
    engine, buy, sells, options = match_scene([('00008', 10.0, 5.0)])
    buy['reserved'] = 0.0
    engine.cash_store.set_reserved_cash('merchant:s', 0.0)
    run(engine, buy, sells, options)
    assert engine.report['trades'] == []
    assert buy['remaining'] == 10.0


def test_quantity_is_limited_by_seller_remaining():
    engine, buy, sells, options = match_scene([('00008', 3.0, 5.0)],
                                              buy_remaining=10.0)
    run(engine, buy, sells, options)
    assert engine.report['trades'][0]['quantity'] == 3.0
    assert buy['remaining'] == 7.0


def test_purchase_quantity_hook_limits_q():
    """Хук движка участвует в расчёте ``q`` и продолжает вызываться."""
    engine, buy, sells, options = match_scene([('00008', 10.0, 5.0)])
    calls = []
    original = Economy.purchase_quantity

    def limited(order, market_sells):
        calls.append(order['id'])
        return 4.0

    engine.purchase_quantity = limited
    run(engine, buy, sells, options)
    assert calls == ['b1']
    assert engine.report['trades'][0]['quantity'] == 4.0
    assert buy['remaining'] == 6.0
    # Прежний хук по умолчанию не ограничивает количество сверх заявки.
    assert original(engine, buy, sells) == 6.0


def test_quantity_keeps_int_semantics():
    engine, buy, sells, options = match_scene([('00008', 10, 5)],
                                              buy_remaining=10, buy_price=5)
    run(engine, buy, sells, options)
    trade = engine.report['trades'][0]
    assert trade['quantity'] == 10 and type(trade['quantity']) is int


# --- 3. tie-break и RNG ---------------------------------------------------
def test_equal_price_group_is_shuffled_by_order_id():
    """Равная цена перемешивается хуком движка, как раньше."""
    engine, buy, sells, options = match_scene(
        [('00008', 5.0, 5.0), ('00009', 5.0, 5.0)], buy_remaining=10.0)
    run(engine, buy, sells, options)
    assert len(engine.report['trades']) == 2
    consumed = [trade['sell_order'] for trade in engine.report['trades']]
    assert sorted(consumed) == ['00008', '00009']
    # Группа равной цены — один вызов shuffled на весь ценовой уровень.
    assert buy['remaining'] == 0


def test_shuffled_hook_is_used_for_each_price_level():
    engine, buy, sells, options = match_scene(
        [('00008', 5.0, 1.0), ('00009', 5.0, 1.0), ('00010', 5.0, 5.0)],
        buy_remaining=20.0)
    seen = []
    original = engine.shuffled

    def traced(values, key=None):
        rows = list(values)
        seen.append(tuple(row[1]['id'] for row in rows))
        return original(rows, key=key)

    engine.shuffled = traced
    run(engine, buy, sells, options)
    assert len(seen) == 2
    assert sorted(seen[0]) == ['00008', '00009']


def test_rng_is_spent_through_engine_shuffled_only():
    engine, buy, sells, options = match_scene(
        [('00008', 5.0, 5.0), ('00009', 5.0, 5.0)])
    before = engine.rng.getstate()
    run(engine, buy, sells, options)
    assert engine.rng.getstate() != before


def test_first_live_cursor_is_kept_in_book():
    engine, buy, sells, options = match_scene([('00008', 10.0, 5.0)])
    book = options.book
    run(engine, buy, sells, options)
    # Следующий обход сдвигает курсор, как в прежнем представлении.
    assert list(offer_boundary.OfferView(book, 'merchant:s', 'p')) == []
    assert book['first_live'] == 1


def test_exhausted_offer_is_not_revived_by_cursor():
    engine, buy, sells, options = match_scene([('00008', 10.0, 5.0)])
    book = options.book
    run(engine, buy, sells, options)
    # Обход уже стоял на этом оффере: курсор не откатывается назад, и
    # возвращённый остаток не воскрешает оффер позади курсора.
    assert list(offer_boundary.OfferView(book, 'merchant:s', 'p')) == []
    engine.order_store.set_remaining(engine.report['orders'][1], 5.0)
    assert list(offer_boundary.OfferView(book, 'merchant:s', 'p')) == []


# --- 4. local_only и провинции -------------------------------------------
def test_local_only_view_filters_other_provinces():
    engine, buy, sells, options = match_scene(
        [('00008', 10.0, 5.0), ('00009', 10.0, 5.0)], buy_remaining=10.0)
    engine.report['orders'][2]['province_id'] = 'other'
    local = engine.offers(engine.accounts['merchant:s'], GOOD, sells, local_only=True)
    run(engine, buy, sells, local)
    assert [trade['sell_order'] for trade in engine.report['trades']] == ['00008']


def test_own_offers_are_not_bought():
    engine, buy, sells, options = match_scene([('00008', 10.0, 5.0)])
    own = sell('own', 'merchant:s', 10.0, 1.0)
    engine.accounts['merchant:s']['stock'] = {GOOD: 10.0}
    engine.accounts['merchant:s']['values'] = {GOOD: 10.0}
    engine.accounts['merchant:s']['stock_reserved'] = {GOOD: 10.0}
    engine.report['orders'].append(own)
    engine.order_store.append(own)
    sells = {GOOD: [own]+engine.report['orders'][1:2]}
    options = engine.offers(engine.accounts['merchant:s'], GOOD, sells)
    run(engine, buy, sells, options)
    assert [trade['sell_order'] for trade in engine.report['trades']] == ['00008']


# --- 5. exact baseline против Python-эталона -----------------------------
def scenario(runner):
    """Три уровня цен, tie-break, остаток и полное исполнение."""
    engine, buy, sells, options = match_scene(
        [('00008', 2.0, 4.0), ('00009', 3.0, 4.0),
         ('00010', 4.0, 5.0), ('00011', 5.0, 6.0)], buy_remaining=7.0)
    run(engine, buy, sells, options, runner)
    return engine, buy


def test_native_matches_python_reference_bit_for_bit():
    native_engine, native_buy = scenario(match_boundary.run_match)
    reference_engine, reference_buy = scenario(match_boundary.run_match_python)
    assert encoded(native_engine, native_buy) == \
        encoded(reference_engine, reference_buy)


def test_native_and_reference_agree_on_order_columns():
    native_engine, native_buy = scenario(match_boundary.run_match)
    reference_engine, reference_buy = scenario(match_boundary.run_match_python)
    native_store, reference_store = native_engine.order_store, reference_engine.order_store
    native_store.sync(), reference_store.sync()
    assert native_store.columns() == reference_store.columns()
    assert native_buy == reference_buy


def test_native_and_reference_agree_with_int_quantities():
    def run_scene(runner):
        engine, buy, sells, options = match_scene(
            [('00008', 3, 4), ('00009', 4, 5)], buy_remaining=5, buy_price=5)
        run(engine, buy, sells, options, runner)
        return engine, buy

    native = encoded(*run_scene(match_boundary.run_match))
    reference = encoded(*run_scene(match_boundary.run_match_python))
    assert native == reference


def test_boundary_binds_compiled_kernel():
    assert match_boundary.NATIVE_MATCH_KERNEL is True
    assert match_boundary.calculate_quantity is KERNEL.calculate_quantity
    assert match_boundary.run_match is not KERNEL.calculate_quantity
    assert match_boundary.run_match is not match_boundary.run_match_python
    assert match_boundary.run_match.__module__.endswith('match_boundary')


def test_fallback_used_when_extension_absent(monkeypatch):
    import importlib

    real_import = importlib.import_module

    def fake_import(name, package=None):
        if name.endswith('match_kernel'):
            raise ImportError("cannot import name 'match_kernel'")
        return real_import(name, package)

    monkeypatch.setattr(importlib, 'import_module', fake_import)
    assert match_boundary.load_match_kernel() is None


# --- 6. хуки движка продолжают работать ----------------------------------
def test_commit_trade_hook_is_used():
    """Сделка исполняется переданным commit_trade, а не копией его тела."""
    calls = []
    engine, buy, sells, options = match_scene([('00008', 10.0, 5.0)])
    commit = trade_boundary.commit_trade

    def traced(*args):
        calls.append(args[2]['id'])
        return commit(*args)

    match_boundary.run_match(engine, buy, options, sells, EPS,
                             engine.cash_store, engine.order_store, traced)
    assert calls == ['00008']


def test_overridden_transfer_is_still_used():
    calls = []
    engine, buy, sells, options = match_scene([('00008', 10.0, 5.0)])
    original = engine.transfer

    def traced(sender, recipient, amount, reason):
        calls.append(reason)
        return original(sender, recipient, amount, reason)

    engine.transfer = traced
    run(engine, buy, sells, options)
    assert calls[:1] == ['Товар']


def test_overridden_use_and_add_stock_are_still_used():
    calls = []
    engine, buy, sells, options = match_scene([('00008', 10.0, 5.0)])
    use_stock, add_stock = engine.use_stock, engine.add_stock

    def traced_use(account, good, quantity):
        calls.append(('use', account, good, quantity))
        return use_stock(account, good, quantity)

    def traced_add(account, good, quantity, value=0):
        calls.append(('add', account, good, quantity, value))
        return add_stock(account, good, quantity, value)

    engine.use_stock = traced_use
    engine.add_stock = traced_add
    run(engine, buy, sells, options)
    assert calls == [('use', 'firm-00008', GOOD, 10.0),
                     ('add', 'merchant:s', GOOD, 10.0, 50.0)]


# --- 7. match целиком: статусы и наблюдение ------------------------------
def test_engine_match_sets_status_and_reason():
    engine, buy, sells, options = match_scene([('00008', 4.0, 5.0)],
                                              buy_remaining=10.0)
    engine.report['orders'] = [buy]+engine.report['orders'][1:]
    engine.match(buy, sells)
    assert buy['status'] == 'partial'
    assert buy['remaining'] == 6.0
    # Причина отказа считается прежним finish_match_observation.
    assert buy['reason'] == 'offer_exhausted'


def test_engine_match_marks_filled_buy():
    engine, buy, sells, options = match_scene([('00008', 10.0, 5.0)])
    engine.report['orders'] = [buy]+engine.report['orders'][1:]
    engine.match(buy, sells)
    assert buy['status'] == 'filled'
    assert buy['remaining'] == 0


def test_engine_match_matches_reference_on_same_month():
    """Один и тот же месяц: native-цикл и Python-эталон дают одно состояние."""
    def run_month(runner):
        engine = build_engine()
        engine.meta['month'] = 1
        buy = buy_order('merchant:s', 10.0, 5.0)
        seller = sell('00008', 'firm-00008', 8.0, 4.0)
        engine.report['orders'] = [buy, seller]
        engine.reset_report()
        engine.order_store.append(buy)
        engine.order_store.append(seller)
        engine.accounts['merchant:s']['stock'] = {}
        engine.accounts['merchant:s']['values'] = {}
        engine.accounts['merchant:s']['balance'] = 1000.0
        engine.accounts['merchant:s']['reserved'] = 0.0
        engine.accounts['firm-00008']['stock'] = {GOOD: 8.0}
        engine.accounts['firm-00008']['values'] = {GOOD: 32.0}
        engine.accounts['firm-00008']['stock_reserved'] = {GOOD: 8.0}
        engine.cash_store.add_reserved_cash('merchant:s', buy['reserved'])
        sells = {GOOD: [seller]}
        buys = [buy]
        for order in buys:
            options = engine.offers(engine.accounts[order['participant']],
                                    order['good'], sells)
            runner(engine, order, options, sells, EPS, engine.cash_store,
                   engine.order_store, trade_boundary.commit_trade)
            engine.order_store.set_status(order, 'filled' if order['remaining'] <= EPS
                                          else 'partial' if order['remaining'] < order['quantity']
                                          else 'unfilled')
        return engine, buy

    native = encoded(*run_month(match_boundary.run_match))
    reference = encoded(*run_month(match_boundary.run_match_python))
    assert native == reference


# --- 8. границы переноса --------------------------------------------------
def test_outer_order_and_book_stay_in_python():
    """Ядро получает один buy и одну книгу: внешний порядок не его дело."""
    import inspect

    parameters = list(inspect.signature(match_boundary.run_match).parameters)
    assert parameters == ['owner', 'buy', 'options', 'sells', 'eps',
                          'cash', 'store', 'commit']


def test_kernel_does_not_copy_orders():
    """Книга и её заявки остаются теми же объектами: копий нет."""
    engine, buy, sells, options = match_scene([('00008', 10.0, 5.0)])
    orders = engine.report['orders']
    book = options.book
    rows = list(book['options'])
    run(engine, buy, sells, options)
    assert engine.report['orders'] is orders
    assert book['options'] == rows
    assert book['source'] is sells[GOOD]
