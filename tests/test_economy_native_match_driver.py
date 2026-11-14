"""Тесты Cython-внешнего цикла покупателей (``run_buyers``).

Предыдущий уровень (``run_match``) обходит книгу одного buyer order. Здесь
проверяется следующий: обход уже перемешанного списка buyer orders вместе с
выбором market book, финализацией статуса и причиной отказа.

Порядок покупателей по-прежнему задаёт Python: ``shuffled(buys)`` вызывается
до ядра, поэтому RNG не меняется. Тесты проверяют:

* точный порядок покупателей и отсутствие перемешивания внутри ядра;
* выбор market book (``offers``) на каждую заявку;
* прежний порядок хуков: ``reprice_input_order`` → ``offers`` →
  ``observe_match`` → внутренний цикл → ``finish_match_observation``;
* статус/reason каждой buy-заявки и её финализацию;
* отсутствие копии orders: ядро перебирает тот же список;
* что ``Economy.match`` и ``Economy.step`` идут через этот драйвер.

Главная проверка — побитовое совпадение native-драйвера и
``run_buyers_python`` на одинаковом состоянии. Тесты не собирают мир целиком:
достаточно маленького движка с несколькими покупателями.
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
DRIVER = match_boundary.load_match_driver()
KERNEL = match_boundary.load_match_kernel()

pytestmark = pytest.mark.skipif(DRIVER is None or KERNEL is None,
                                reason='Cython-расширения match_driver/match_kernel не собраны')

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


def sell(identity, participant, remaining, price, province_id='p'):
    """Живая sell-заявка: обычный словарь, как у настоящего движка."""
    return dict(id=identity, participant=participant, good=GOOD,
        quantity=remaining, remaining=remaining, price=price, side='sell',
        purpose='Продажа запасов', status='active', reason='', expires=1,
        final_consumption=False, state_id='s', province_id=province_id,
        tile_id='t', reserved=0, backing=remaining)


def buy_order(identity, participant, remaining, price, quantity=None,
              purpose='Товар'):
    """Buy-заявка с резервом, как её ставит ``Economy.order``."""
    return dict(id=identity, participant=participant, good=GOOD,
        quantity=remaining if quantity is None else quantity,
        remaining=remaining, price=price, side='buy', purpose=purpose,
        status='active', reason='', expires=1, final_consumption=False,
        state_id='s', province_id='p', tile_id='t',
        reserved=remaining*price, backing=remaining*price)


def seller_account(engine, participant, remaining, price):
    """Счёт продавца с закреплённым товаром: сделка должна быть исполнимой."""
    account = engine.accounts.setdefault(participant, dict(
        id=participant, kind='firm', state_id='s', province_id='p',
        tile_id='t', balance=100.0, reserved=0.0,
        stock={}, values={}, stock_reserved={}))
    account['stock'] = {GOOD: remaining}
    account['values'] = {GOOD: remaining*price}
    account['stock_reserved'] = {GOOD: remaining}
    return account


def buyer_account(engine, participant, cash=1000.0):
    """Счёт покупателя без стартового склада: видно только купленное здесь."""
    account = engine.accounts.setdefault(participant, dict(
        id=participant, kind='merchant', state_id='s', province_id='p',
        tile_id='t', balance=cash, reserved=0.0,
        stock={}, values={}, stock_reserved={}))
    account.update(balance=cash, reserved=0.0, stock={}, values={})
    return account


def scene(buyers, sellers, detail='none', seed=17):
    """Движок, живые заявки и книга: ``buyers``/``sellers`` — (id, remaining, price)."""
    engine = build_engine(seed=seed, detail=detail)
    engine.meta['month'] = 1
    orders = []
    sells = {GOOD: []}
    for identity, remaining, price in buyers:
        participant = f'buyer-{identity}'
        buyer_account(engine, participant)
        order = buy_order(identity, participant, remaining, price)
        orders.append(order)
        engine.cash_store.add_reserved_cash(participant, order['reserved'])
    for identity, remaining, price in sellers:
        participant = f'firm-{identity}'
        seller_account(engine, participant, remaining, price)
        order = sell(identity, participant, remaining, price)
        orders.append(order)
        sells[GOOD].append(order)
    engine.report['orders'] = orders
    engine.report['trades'] = []
    engine.report['flows'] = []
    engine.reset_report()
    for order in orders:
        engine.order_store.append(order)
    return engine, [o for o in orders if o['side'] == 'buy'], sells


def run(engine, buys, sells, runner=None, inner=None, local_only=False):
    """Выбранная реализация внешнего цикла с теми же аргументами, что и в V3."""
    function = runner or match_boundary.run_buyers
    return function(engine, buys, sells, EPS, trade_boundary.commit_trade,
                    inner or match_boundary.run_match, local_only)


def snapshot(engine):
    """Состояние, которое меняет внешний цикл: деньги, товар, заявки, RNG."""
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
        boards=copy.deepcopy(engine.report['market_boards']),
        orders=[dict(order) for order in engine.report['orders']],
        columns=store.columns(),
        demand_revision=dict(engine._demand_revision),
        trades_by_seller=dict(engine._trades_by_seller),
    )


def encoded(engine):
    """JSON с сортировкой ключей: сравнение без учёта порядка словарей."""
    return json.dumps(snapshot(engine), sort_keys=True, default=str)


# --- 1. порядок покупателей и RNG -----------------------------------------
def test_buyers_are_processed_in_given_order():
    engine, buys, sells = scene([('b1', 10.0, 5.0), ('b2', 10.0, 5.0)],
                                [('00008', 10.0, 5.0), ('00009', 10.0, 5.0)])
    run(engine, [buys[1], buys[0]], sells)
    # Первым идёт b2: именно его сделка попадает в отчёт первой.
    assert [trade['buy_order'] for trade in engine.report['trades']] == ['b2', 'b1']


def test_driver_does_not_shuffle_the_buyer_list():
    """Порядок задаёт Python: ядро перемешивает только группы офферов."""
    engine, buys, sells = scene([('b1', 4.0, 5.0), ('b2', 4.0, 5.0)],
                                [('00008', 5.0, 5.0), ('00009', 5.0, 5.0)])
    rows_seen = []
    original = engine.shuffled

    def traced(values, key=None):
        rows = list(values)
        rows_seen.append(rows)
        return original(rows, key=key)

    engine.shuffled = traced
    run(engine, list(buys), sells)
    # Каждый покупатель заново обходит книгу: два вызова на две заявки, и оба
    # раза перемешивается группа равной цены, а не список покупателей.
    assert len(rows_seen) == 2
    for rows in rows_seen:
        assert all(isinstance(row, tuple) and len(row) == 3 for row in rows)
        assert {row[1]['id'] for row in rows} == {'00008', '00009'}


def test_shuffled_buyer_order_from_engine_is_kept():
    """``shuffled(buys)`` вызывается до ядра: тот же порядок, что и в V3."""
    engine, buys, sells = scene([('b1', 4.0, 5.0), ('b2', 4.0, 5.0),
                                 ('b3', 4.0, 5.0)],
                                [('00008', 12.0, 5.0)])
    ordered = engine.shuffled(buys)
    expected = [order['id'] for order in ordered]
    run(engine, ordered, sells)
    assert [trade['buy_order'] for trade in engine.report['trades']] == expected


def test_rng_state_matches_python_reference():
    """Расход RNG: тот же порядок вызовов, тот же конечный state."""
    def rng(runner):
        engine, buys, sells = scene([('b1', 5.0, 5.0), ('b2', 5.0, 5.0)],
                                    [('00008', 5.0, 5.0), ('00009', 5.0, 5.0)])
        run(engine, list(buys), sells, runner=runner)
        return engine.rng.getstate()

    assert rng(match_boundary.run_buyers) == rng(match_boundary.run_buyers_python)


# --- 2. market book и порядок хуков ---------------------------------------
def test_offers_hook_is_called_per_buyer():
    engine, buys, sells = scene([('b1', 4.0, 5.0), ('b2', 4.0, 5.0)],
                                [('00008', 10.0, 5.0)])
    seen = []
    original = engine.offers

    def traced(buyer, good, market_sells, local_only=False):
        seen.append((buyer['id'], good, local_only))
        return original(buyer, good, market_sells, local_only=local_only)

    engine.offers = traced
    run(engine, buys, sells)
    assert seen == [('buyer-b1', GOOD, False), ('buyer-b2', GOOD, False)]


def test_local_only_is_forwarded_to_book_selection():
    engine, buys, sells = scene([('b1', 10.0, 5.0)], [('00008', 10.0, 5.0)])
    sells[GOOD][0]['province_id'] = 'other'
    run(engine, buys, sells, local_only=True)
    # Локальный проход не видит чужую провинцию: заявка остаётся без сделки.
    assert engine.report['trades'] == []
    assert buys[0]['status'] == 'unfilled'


def test_hooks_are_called_in_previous_order():
    engine, buys, sells = scene([('b1', 10.0, 5.0)], [('00008', 10.0, 5.0)])
    calls = []
    originals = dict(
        reprice=engine.reprice_input_order,
        offers=engine.offers,
        observe=engine.observe_match,
        finish=engine.finish_match_observation)

    def reprice(buy, market_sells):
        calls.append('reprice')
        return originals['reprice'](buy, market_sells)

    def offers(buyer, good, market_sells, local_only=False):
        calls.append('offers')
        return originals['offers'](buyer, good, market_sells, local_only=local_only)

    def observe(buy, market_sells, options):
        calls.append('observe')
        return originals['observe'](buy, market_sells, options)

    def finish(buy, market_sells, observation):
        calls.append('finish')
        return originals['finish'](buy, market_sells, observation)

    engine.reprice_input_order = reprice
    engine.offers = offers
    engine.observe_match = observe
    engine.finish_match_observation = finish
    run(engine, buys, sells)
    assert calls == ['reprice', 'offers', 'observe', 'finish']


def test_inner_loop_is_the_existing_cython_kernel():
    """Внутренний цикл приходит аргументом: копии обхода книги нет."""
    engine, buys, sells = scene([('b1', 10.0, 5.0)], [('00008', 10.0, 5.0)])
    calls = []

    def inner(owner, buy, options, market_sells, eps, cash, store, commit):
        calls.append(buy['id'])
        return KERNEL.run_match(owner, buy, options, market_sells, eps, cash,
                                store, commit)

    run(engine, buys, sells, inner=inner)
    assert calls == ['b1']
    assert engine.report['trades'][0]['buy_order'] == 'b1'


# --- 3. статус, reason и финализация --------------------------------------
def test_each_buy_gets_status_and_reason():
    engine, buys, sells = scene([('b1', 10.0, 5.0), ('b2', 4.0, 5.0)],
                                [('00008', 6.0, 5.0)])
    run(engine, buys, sells)
    partial, unfilled = buys
    assert (partial['status'], partial['remaining']) == ('partial', 4.0)
    assert partial['reason'] == 'offer_exhausted'
    assert unfilled['status'] == 'unfilled'
    assert unfilled['reason'] == 'offer_exhausted'


def test_cancelled_buy_is_skipped_without_hooks():
    engine, buys, sells = scene([('b1', 10.0, 5.0)], [('00008', 10.0, 5.0)])
    buys[0]['status'] = 'cancelled'
    calls = []
    engine.offers = lambda *args, **kwargs: calls.append('offers')
    run(engine, buys, sells)
    assert calls == []
    assert engine.report['trades'] == []


def test_status_is_written_through_order_store():
    engine, buys, sells = scene([('b1', 4.0, 5.0)], [('00008', 10.0, 5.0)])
    run(engine, buys, sells)
    store = engine.order_store
    slot = store.slot(buys[0])
    assert buys[0]['status'] == 'filled'
    assert store.columns()['status'][slot] == 'filled'


def test_none_buy_is_ignored():
    engine, buys, sells = scene([('b1', 4.0, 5.0)], [('00008', 10.0, 5.0)])
    run(engine, [None, buys[0]], sells)
    assert engine.report['trades'][0]['buy_order'] == 'b1'


# --- 4. exact baseline против Python-эталона ------------------------------
def run_scene(runner):
    """Один и тот же сценарий выбранной реализацией внешнего цикла."""
    engine, buys, sells = scene(
        [('b1', 3.0, 6.0), ('b2', 5.0, 5.0)],
        [('00008', 2.0, 4.0), ('00009', 3.0, 4.0), ('00010', 4.0, 5.0)])
    runner(engine, list(buys), sells, EPS, trade_boundary.commit_trade,
           match_boundary.run_match)
    return engine


def test_native_driver_matches_python_reference_bit_for_bit():
    assert encoded(run_scene(match_boundary.run_buyers)) == \
        encoded(run_scene(match_boundary.run_buyers_python))


def test_native_and_reference_agree_on_statuses_and_reasons():
    def statuses(runner):
        return [(order['id'], order['status'], order['reason'])
                for order in run_scene(runner).report['orders']
                if order['side'] == 'buy']

    assert statuses(match_boundary.run_buyers) == \
        statuses(match_boundary.run_buyers_python)


def test_native_and_reference_agree_with_detail_deep():
    def run_deep(runner):
        engine, buys, sells = scene([('b1', 4.0, 5.0), ('b2', 4.0, 5.0)],
                                    [('00008', 10.0, 5.0)], detail='deep')
        runner(engine, list(buys), sells, EPS, trade_boundary.commit_trade,
               match_boundary.run_match)
        return engine

    assert encoded(run_deep(match_boundary.run_buyers)) == \
        encoded(run_deep(match_boundary.run_buyers_python))


def test_many_buyers_match_reference_exactly():
    """Двенадцать покупателей и общая книга: порядок, остатки, RNG и отчёт."""
    buyers = [(f'b{index:02d}', 2.0+index % 3, 3.0+index % 4) for index in range(12)]
    sellers = [(f'{index:05d}', 1.0+index % 2, 3.0+index % 5) for index in range(15)]

    def run_many(runner):
        engine, orders, sells = scene(buyers, sellers)
        runner(engine, list(orders), sells, EPS, trade_boundary.commit_trade,
               match_boundary.run_match)
        return engine

    assert encoded(run_many(match_boundary.run_buyers)) == \
        encoded(run_many(match_boundary.run_buyers_python))


def test_single_buyer_entry_point_matches_reference():
    def run_one(function):
        engine, buys, sells = scene([('b1', 4.0, 5.0), ('b2', 4.0, 5.0)],
                                    [('00008', 10.0, 5.0)])
        function(engine, buys[0], sells, EPS, trade_boundary.commit_trade,
                 match_boundary.run_match)
        return engine

    assert encoded(run_one(match_boundary.run_buyer)) == \
        encoded(run_one(match_boundary.run_buyer_python))


def test_boundary_binds_compiled_driver():
    assert match_boundary.NATIVE_MATCH_DRIVER is True
    assert match_boundary.run_buyers is DRIVER.run_buyers
    assert match_boundary.run_buyer is DRIVER.run_buyer
    assert match_boundary.run_buyers is not match_boundary.run_buyers_python
    assert match_boundary.run_buyers.__module__.endswith('match_driver')


def test_fallback_used_when_driver_absent(monkeypatch):
    import importlib

    real_import = importlib.import_module

    def fake_import(name, package=None):
        if name.endswith('match_driver'):
            raise ImportError("cannot import name 'match_driver'")
        return real_import(name, package)

    monkeypatch.setattr(importlib, 'import_module', fake_import)
    assert match_boundary.load_match_driver() is None


# --- 5. хуки движка продолжают работать ----------------------------------
def test_purchase_quantity_hook_is_used_per_buyer():
    engine, buys, sells = scene([('b1', 10.0, 5.0), ('b2', 10.0, 5.0)],
                                [('00008', 10.0, 5.0), ('00009', 10.0, 5.0)])
    calls = []
    engine.purchase_quantity = lambda order, market_sells: (
        calls.append(order['id']) or 2.0)
    run(engine, buys, sells)
    # Внутренний цикл вызывает хук на каждом живом оффере, как и раньше.
    assert set(calls) == {'b1', 'b2'}


def test_overridden_transfer_is_still_used():
    engine, buys, sells = scene([('b1', 10.0, 5.0)], [('00008', 10.0, 5.0)])
    calls = []
    original = engine.transfer

    def traced(sender, recipient, amount, reason):
        calls.append(reason)
        return original(sender, recipient, amount, reason)

    engine.transfer = traced
    run(engine, buys, sells)
    assert calls[:1] == ['Товар']


def adaptive_scene(runner):
    """Адаптивная household-заявка: цена пересматривается до потолка бюджета."""
    engine, buys, sells = scene([('b1', 4.0, 1.0)], [('00008', 10.0, 4.0)])
    buy = buys[0]
    buy.update(adaptive_household_budget=True, final_consumption=True)
    engine.accounts[buy['participant']]['kind'] = 'household'
    runner(engine, list(buys), sells, EPS, trade_boundary.commit_trade,
           match_boundary.run_match)
    return engine, buy


def test_adaptive_household_price_is_recomputed():
    """Блок адаптивной цены household-заявки перенесён без изменений."""
    engine, buy = adaptive_scene(match_boundary.run_buyers)
    # Потолок котировки выше лимита: цена поднята, исходный лимит сохранён,
    # а заявка куплена целиком по новой цене.
    assert buy['price'] > 1.0
    assert buy['initial_limit'] == 1.0
    assert buy['status'] == 'filled'
    assert engine.accounts[buy['participant']]['stock'][GOOD] == 4.0


def test_adaptive_household_price_matches_reference():
    assert encoded(adaptive_scene(match_boundary.run_buyers)[0]) == \
        encoded(adaptive_scene(match_boundary.run_buyers_python)[0])


# --- 6. вызов из движка и границы переноса --------------------------------
def test_engine_match_delegates_to_native_driver():
    engine, buys, sells = scene([('b1', 4.0, 5.0)], [('00008', 10.0, 5.0)])
    calls = []
    original = match_boundary.run_buyer

    def traced(owner, buy, market_sells, eps, commit, inner, local_only=False):
        calls.append((buy['id'], local_only))
        return original(owner, buy, market_sells, eps, commit, inner, local_only)

    match_boundary.run_buyer = traced
    try:
        engine.match(buys[0], sells)
        engine.match(buys[0], sells, local_only=True)
    finally:
        match_boundary.run_buyer = original
    assert calls == [('b1', False), ('b1', True)]
    assert buys[0]['status'] == 'filled'


def test_engine_match_runs_the_same_hooks_as_before():
    """``match`` остаётся точкой входа: наблюдение и статус на месте."""
    engine, buys, sells = scene([('b1', 4.0, 5.0)], [('00008', 10.0, 5.0)],
                                detail='deep')
    buy = buys[0]
    engine.match(buy, sells)
    assert buy['status'] == 'filled'
    assert buy['match_observation']['reason_code'] == 'filled'
    assert engine.report['market_boards'][0]['board_id']


def test_driver_does_not_copy_orders():
    """Ядро перебирает тот же список: копий заявок нет."""
    engine, buys, sells = scene([('b1', 4.0, 5.0), ('b2', 4.0, 5.0)],
                                [('00008', 10.0, 5.0)])
    orders = engine.report['orders']
    ordered = list(buys)
    run(engine, ordered, sells)
    assert engine.report['orders'] is orders
    assert all(left is right for left, right in zip(ordered, buys))


def test_driver_signature_stays_on_boundary():
    import inspect

    buyers = list(inspect.signature(match_boundary.run_buyers).parameters)
    assert buyers == ['owner', 'buys', 'sells', 'eps', 'commit', 'inner',
                      'local_only']
    buyer = list(inspect.signature(match_boundary.run_buyer).parameters)
    assert buyer == ['owner', 'buy', 'sells', 'eps', 'commit', 'inner',
                     'local_only']
