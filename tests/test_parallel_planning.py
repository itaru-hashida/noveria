"""Регрессии ожиданий и бюджета экспериментального планировщика."""
import numpy as np
import pytest

from modules.economy import Economy
from shadow import agent_planner, agent_snapshot


@pytest.fixture
def market(service):
    service.initialize_economy()
    engine = Economy.load(service.db)
    arrays, meta = agent_snapshot.build(engine, {f: 10000 for f in engine.enterprises})
    parameters = tuple(engine.cfg[k] for k in
                       ('transport_rate', 'wage', 'consumption_tax', 'markup', 'enterprise_overhead_rate'))
    return engine, arrays, meta, parameters


def test_history_survives_empty_order_book(market):
    _, arrays, meta, parameters = market
    agent = meta['firm_good'].index('flour')
    arrays['agent_stock'][agent] = 0
    arrays['agent_available_stock'][agent] = 0
    arrays['agent_history_count'][agent] = 1
    arrays['agent_history'][agent, 0] = (20, 20, 0)
    arrays['agent_forecast'][agent] = 20
    trace = {}
    intents = agent_planner.plan(arrays, agent, *parameters, trace=trace)
    assert trace['observed'] == 0
    assert trace['forecast'] == pytest.approx(20)
    assert trace['target'] > 0
    assert any(i[0] == 'buy' and i[2] > 0 for i in intents)
    assert intents[0][5] == trace['target']


def test_budget_protects_payroll_and_respects_existing_inputs(market):
    _, arrays, meta, parameters = market
    agent = meta['firm_good'].index('flour')
    arrays['agent_stock'][agent] = 0
    arrays['agent_available_stock'][agent] = 0
    arrays['agent_budget'][agent] = 2.0
    arrays['agent_forecast'][agent] = 100
    arrays['agent_history_count'][agent] = 1
    arrays['agent_history'][agent, 0] = (100, 100, 0)
    trace = {}
    intents = agent_planner.plan(arrays, agent, *parameters, trace=trace)
    spend = sum(i[2]*i[3] for i in intents if i[0] == 'buy')
    assert spend+trace['wage_reserve']+trace['overhead'] <= 2.0+1e-7
    assert intents[0][5] == trace['target'] < trace['desired']


def test_input_shortage_preserves_forecast_but_unsold_stock_stops_output(market):
    _, arrays, meta, _ = market
    agent = meta['firm_good'].index('flour')
    arrays['agent_history_count'][agent] = 1
    arrays['agent_history'][agent, 0] = (0, 0, 1)
    arrays['agent_forecast'][agent] = 20
    assert agent_planner.expectations(arrays, agent, 0) == pytest.approx(20)
    arrays['agent_history'][agent, 0, 2] = 0
    assert agent_planner.expectations(arrays, agent, 0) < 20


def test_new_firm_uses_public_history(market):
    _, arrays, meta, parameters = market
    agent = meta['firm_good'].index('flour')
    good = int(arrays['agent_good'][agent])
    arrays['agent_history_count'][agent] = 0
    arrays['agent_stock'][agent] = 0
    arrays['agent_available_stock'][agent] = 0
    arrays['public_share'][good] = 10
    trace = {}
    agent_planner.plan(arrays, agent, *parameters, trace=trace)
    assert trace['forecast'] == 10
    assert trace['target'] > 0


@pytest.mark.parametrize("workers", [0, 2])
def test_full_month_replays_recorded_fifo_and_persists(service, workers):
    from shadow.turn import AgentTurn
    service.initialize_economy()
    left = Economy.load(service.db)
    right = Economy.load(service.db)
    from concurrent.futures import ProcessPoolExecutor
    import multiprocessing
    from contextlib import ExitStack
    from shadow import sandbox_mp
    sandbox_mp.enable_multiprocessing()
    with ExitStack() as stack:
        pool = stack.enter_context(ProcessPoolExecutor(max_workers=workers,
            mp_context=multiprocessing.get_context('spawn'))) if workers else None
        planner = AgentTurn(pool)
        left.step(enterprise_planner=planner)
    replay = AgentTurn(None, replay=planner.log)
    right.step(enterprise_planner=replay)
    assert left.accounts == right.accounts
    assert left.enterprises == right.enterprises
    assert left.meta == right.meta
    assert left.report == right.report
    assert all(left.report['invariants'].values())
    with service.db.transaction():
        left.save(service.db)
    loaded = Economy.load(service.db)
    assert loaded.meta['month'] == left.meta['month']
    assert loaded.accounts == left.accounts
    assert loaded.enterprises == left.enterprises
    assert any('agent_sales_forecast' in f for f in loaded.enterprises.values())


def test_chain_restores_after_supply_halt(service):
    from shadow.turn import AgentTurn
    service.initialize_economy()
    engine = Economy.load(service.db)
    original_sync = engine.sync_locations

    def sync_with_halt():
        original_sync()
        for firm in engine.enterprises.values():
            if firm['good'] == 'grain':
                firm['suspended'] = 3 <= engine.meta['month'] <= 5
    engine.sync_locations = sync_with_halt
    made = []
    for month in range(12):
        report = engine.step(enterprise_planner=AgentTurn(None))
        assert all(report['invariants'].values())
        made.append(dict(report['production']))
    assert all(row.get('grain', 0) == 0 for row in made[2:5])
    assert any(row.get('grain', 0) > 0 for row in made[5:])
    assert any(row.get('flour', 0) > 0 for row in made[6:])
    assert any(row.get('bread', 0) > 0 for row in made[7:])


def test_initial_probe_is_not_repeated_without_sales(market):
    _, arrays, meta, _ = market
    agent = meta['firm_good'].index('flour')
    arrays['agent_history_count'][agent] = 0
    arrays['public_share'][int(arrays['agent_good'][agent])] = 0
    arrays['agent_initialized'][agent] = 1
    arrays['agent_forecast'][agent] = 20
    assert agent_planner.expectations(arrays, agent, 0) == pytest.approx(13)


def _curve_book(levels):
    """Книга из уровней [(landed, quantity)] на двух площадках.

    Площадка покупателя — 0. Первая площадка продаёт по цене, дающей landed
    ровно `landed`; доставка нулевая, чтобы тест не зависел от карты.
    """
    n_goods = 3
    prices = np.array([price for price, _ in levels]+[1.0], dtype=np.float64)
    remaining = np.array([quantity for _, quantity in levels]+[0.0], dtype=np.float64)
    tiles = np.zeros(len(levels)+1, dtype=np.int32)
    goods = np.full(len(levels)+1, 0, dtype=np.int32)
    book = {
        'good_weight': np.zeros(n_goods), 'distance': np.zeros((2, 2)),
        'offer_ptr': np.array([0, len(levels)+1, len(levels)+1, len(levels)+1]),
        'offer_price': prices, 'offer_remaining': remaining,
        'offer_tile': tiles, 'offer_good': goods,
        'reachable': np.ones((2, 2), dtype=np.uint8),
        'good_price': np.ones(n_goods), 'public_price': np.ones(n_goods),
    }
    return book


def test_offer_curve_sorts_by_landed_and_merges_levels():
    book = _curve_book([(1.5, 100.0), (1.2, 50.0), (1.5, 25.0)])
    curve = agent_planner.offer_curve(book, 0, 0, 0.0)
    assert [price for price, _ in curve] == pytest.approx([1.2, 1.5])
    assert [quantity for _, quantity in curve] == pytest.approx([50.0, 125.0])


def test_bid_covers_second_level_after_cheap_one_is_taken():
    """Предел заявки — priced последней нужной единицы, не лучшей цены."""
    book = _curve_book([(1.2, 50.0), (1.5, 100.0)])
    quantity, bid, available, affordable = agent_planner.input_plan(
        book, 0, 0, wanted=120.0, ceiling=9.0, cash=1000.0, rate=0.0)
    assert quantity == pytest.approx(120.0)
    assert available == pytest.approx(150.0)
    assert bid == pytest.approx(1.5)


def test_bid_is_capped_by_margin_ceiling():
    book = _curve_book([(1.2, 50.0), (1.5, 100.0)])
    quantity, bid, _, affordable = agent_planner.input_plan(
        book, 0, 0, wanted=120.0, ceiling=1.3, cash=1000.0, rate=0.0)
    assert quantity == pytest.approx(50.0)
    assert bid == pytest.approx(1.2)
    assert affordable == pytest.approx(50.0)


def test_bid_is_capped_by_cash_and_returns_bought_quantity():
    book = _curve_book([(1.0, 100.0), (2.0, 100.0)])
    quantity, bid, _, _ = agent_planner.input_plan(
        book, 0, 0, wanted=150.0, ceiling=9.0, cash=150.0, rate=0.0)
    # 100 по 1.0 плюс 25 по 2.0 = 150 денег; предел — цена последней единицы.
    assert quantity == pytest.approx(125.0)
    assert bid == pytest.approx(2.0)


def test_bid_splits_level_between_rivals():
    """Один уровень делят несколько покупателей: иначе предел занижен."""
    book = _curve_book([(1.2, 50.0), (1.5, 100.0)])
    alone = agent_planner.input_plan(book, 0, 0, wanted=60.0, ceiling=9.0,
                                     cash=1000.0, rate=0.0, rivals=1)
    shared = agent_planner.input_plan(book, 0, 0, wanted=60.0, ceiling=9.0,
                                      cash=1000.0, rate=0.0, rivals=10)
    # Без конкурентов агент берёт весь объём с двух уровней.
    assert alone[0] == pytest.approx(60.0)
    assert alone[1] == pytest.approx(1.5)
    # При десяти конкурентах на дешёвый уровень приходится 5 единиц, остальное
    # берётся со второго уровня, поэтому предел тоже 1.5, но объём меньше.
    assert shared[0] < alone[0]
    assert shared[1] == pytest.approx(1.5)


def test_plan_reports_purchase_reasons_and_respects_wallet(market):
    _, arrays, meta, parameters = market
    agent = meta['firm_good'].index('flour')
    arrays['agent_stock'][agent] = 0
    arrays['agent_available_stock'][agent] = 0
    arrays['agent_budget'][agent] = 3.0
    arrays['agent_forecast'][agent] = 100
    arrays['agent_history_count'][agent] = 1
    arrays['agent_history'][agent, 0] = (100, 100, 0)
    trace = {}
    intents = agent_planner.plan(arrays, agent, *parameters, trace=trace)
    spend = sum(i[2]*i[3] for i in intents if i[0] == 'buy')
    assert spend+trace['wage_reserve']+trace['overhead'] <= 3.0+1e-7
    assert trace['reasons'], 'закупки без разбора: не видно, что ограничило'
    for row in trace['reasons']:
        assert row['bought'] <= row['needed']+1e-7
        assert row['bought'] <= row['available']+1e-7


def test_reusing_market_books_preserves_snapshot_and_input_quotes(market):
    from shadow.runner import prepare_market
    engine, _, _, _ = market
    sells = prepare_market(engine)
    budget = {firm: engine.free_cash(firm) for firm in engine.enterprises}
    original, meta = agent_snapshot.build(engine, budget)
    reused, _ = agent_snapshot.build(engine, budget, sells=sells)
    for name in original:
        np.testing.assert_array_equal(original[name], reused[name])
    for i, fid in enumerate(meta['firms']):
        firm = engine.enterprises[fid]
        recipe = engine.goods[firm['good']]
        for good in recipe['inputs'].keys() | recipe.get('equipment', {}).keys():
            assert reused['agent_input_price'][i, meta['good_index'][good]] == engine.reference_price(
                engine.accounts[fid], good, sells)
