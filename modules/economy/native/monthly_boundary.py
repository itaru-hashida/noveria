"""Единственная точка входа месяца: native-orchestration или Python-эталон.

Python вызывает ``monthly_step`` один раз за месяц. Ядро
(``monthly_driver``) последовательно выполняет фазы месяца в прежнем порядке
и возвращает живой ``engine.report``.

Эталонная Python-реализация живёт в этом же модуле: она нужна как fallback,
если расширение не собрано, и как образец для differential-тестов. Чтобы у
правила не было двух расходящихся копий, эталон и ядро получают один и тот же
движок и одни и те же аргументы; ядро не хранит состояние между вызовами.

Модуль не импортирует ``modules.economy.v3``: движок приходит аргументом,
поэтому циклического импорта нет.
"""

import importlib

from .monthly_hooks import MonthlyHooks


def monthly_step_python(engine, eps, planning_mode='v3', investment_mode='v3',
                        investment_workers=1, native_mode='off',
                        enterprise_planner=None):
    """Эталон прежнего тела ``Economy.step``: тот же порядок фаз и тот же RNG.

    Это не второй движок и не копия состояния: функция работает с живым
    ``engine``, его хранилищами и его отчётом, ровно как ядро. Хуки месяца она
    берёт те же, что и ядро, поэтому differential-тест сравнивает ветки, а не
    две копии одного кода.
    """
    import time

    started = time.perf_counter()
    timings = {}
    hooks = getattr(engine, 'monthly_hooks', None) or MonthlyHooks(engine)
    engine.__dict__.pop('_planning_orders', None)
    engine.__dict__.pop('_recovery_quotes', None)
    engine.__dict__.pop('_stock_sales_cache', None)
    engine.activate_rules()
    engine.accounts = dict(sorted(engine.accounts.items()))
    engine.enterprises = dict(sorted(engine.enterprises.items()))
    engine.meta['month'] += 1
    engine.reset_report()
    engine.report.update(rules_version=3, investment_decisions=[])
    engine.sync_locations()
    before = engine.total_stock()
    engine.report['opening_balances'] = {a['id']: a['balance'] for a in engine.accounts.values()}
    engine.report['gold_before'] = engine.gold()
    prior_cash = {a['id']: a['balance'] for a in engine.accounts.values() if a['kind'] == 'household'}
    # Тот же хук, что и в native-ветке: деньги меняет CashStore, склад —
    # StockStore, защита кассы остаётся полем представления.
    hooks.reset_month_reserves(engine.accounts)
    if native_mode == 'cython':
        engine.stock_store.sync()
    existing = list(engine.enterprises.values())
    from .enterprise_reset_boundary import reset_enterprises
    reset_enterprises(existing)
    world = engine.world_state if native_mode == 'cython' else None
    completed = False
    if world is not None:
        world.begin_step(engine)
    try:
        if native_mode == 'cython':
            from .update_prices_boundary import run_native_public_projects, run_update_prices
            if planning_mode == 'lagged_batch':
                run_update_prices(engine)
            else:
                run_native_public_projects(engine)
        else:
            engine.update_prices()
        lagged_plans = None
        lagged_firms = ()
        if planning_mode == 'lagged_batch':
            from shadow.lagged_batch import apply_plans, build_snapshot, plan_all, publish_sales
            phase_started = time.perf_counter()
            snapshot = build_snapshot(engine)
            lagged_plans = plan_all(snapshot)
            lagged_firms = snapshot.eligible
            timings['lagged_snapshot_planning'] = time.perf_counter()-phase_started
            engine.report['planning_mode'] = planning_mode
        elif native_mode != 'cython':
            engine.prepare_recovery_quotes()
        if native_mode == 'cython' and planning_mode != 'lagged_batch':
            sells = engine.native_market['sells']
        else:
            sells = engine.sell_orders(exclude=lagged_firms)
        if planning_mode == 'lagged_batch':
            publish_sales(engine, lagged_plans, sells)
        detail = engine.report_detail()
        if native_mode == 'cython' and planning_mode != 'lagged_batch':
            households = engine.native_households['households']
            food_trace = engine.native_households['food_trace']
        else:
            households = engine.shuffled(a for a in engine.accounts.values() if a['kind'] == 'household')
            food_trace = None
            if engine.FOOD_DIAGNOSTICS and detail == 'deep':
                from ..food_diagnostics import FoodDiagnostics
                food_trace = FoodDiagnostics(engine, households, sells)
            engine.household_requests(households, engine.cfg['basic_needs'], 'Базовые нужды', sells)
            engine.household_requests(households, engine.cfg['extra_needs'], 'Дополнительные нужды', sells)
        if native_mode != 'cython' or planning_mode == 'lagged_batch':
            engine.owner_requests(sells)
        if planning_mode != 'lagged_batch' and native_mode != 'cython':
            engine.recover_enterprises(sells)
        if native_mode != 'cython':
            engine.public_project_requests(sells)
        if investment_mode == 'snapshot_batch':
            from shadow.investment_batch import run_snapshot_batch
            timings.update(run_snapshot_batch(engine, sells, workers=investment_workers))
            engine.report['investment_mode'] = investment_mode
        elif investment_mode == 'async_snapshot_batch':
            from shadow.investment_batch import run_async_snapshot_batch
            async_snapshot = run_async_snapshot_batch(engine, sells)
            engine.report['investment_mode'] = investment_mode
        elif native_mode == 'cython':
            from .update_prices_boundary import run_native_project_requests
            run_native_project_requests(engine, sells)
        else:
            engine.project_requests(sells)
        timings['preparation'] = time.perf_counter()-started
        phase_started = time.perf_counter()
        if planning_mode == 'lagged_batch':
            apply_plans(engine, lagged_plans)
            timings['lagged_apply'] = time.perf_counter()-phase_started
        elif enterprise_planner is None:
            engine.plan_enterprises(existing, sells)
        else:
            enterprise_planner(engine, existing, sells)
        timings['planning_execution'] = time.perf_counter()-phase_started
        phase_started = time.perf_counter()
        buys = [o for o in engine.report['orders'] if o['side'] == 'buy']
        for buy in engine.shuffled([o for o in buys if o['purpose'] == 'Базовые нужды']):
            engine.match(buy, sells, local_only=True)
        from . import match_boundary as _match
        from . import trade_boundary as _trade
        _match.run_buyers(engine, engine.shuffled(buys), sells, eps,
            _trade.commit_trade, _match.run_match)
        if engine.HOUSEHOLD_TOP_UP_REVISION:
            engine.household_top_up(households, sells)
        engine.record_market()
        if food_trace:
            food_trace.clearing(engine, sells)
        engine._planning_orders = [engine._planning_order_snapshot(order)
            for order in engine.report['orders']]
        planning_sells = engine._planning_market_snapshot(sells)
        # Истечение заявок — тот же хук, что и в ядре: статус меняет OrderStore,
        # тексты причин остаются полями отчёта.
        hooks.expire_orders()
        timings['market_tail'] = time.perf_counter()-phase_started
        phase_started = time.perf_counter()
        engine.reserve_expedited_construction_labor()
        engine.produce(existing, planning_sells)
        engine.advance_public_projects()
        engine.finish_projects()
        engine.consume_owner_purchases()
        from . import consumption_boundary as _consumption
        _consumption.consume_households(engine, households)
        from . import report_history_boundary as _report_history
        _report_history.update_sales_history(engine, eps)
        _report_history.update_stock_sales_history(engine)
        hooks.protect_balances(engine.accounts)
        engine.settle(list(engine.enterprises.values()), households)
        engine.pay_dividends(planning_sells)
        engine.update_demography()
        engine.apply_migration()
        hooks.apply_project_reserves(engine.accounts, planning_sells)
        hooks.extend_diagnostics(detail)
        timings['production_settlement'] = time.perf_counter()-phase_started
        phase_started = time.perf_counter()
        if detail == 'none':
            engine.update_welfare()
        else:
            engine.summarize(prior_cash)
            balances, money_shares = _report_history.build_report_aggregates(engine, engine.report)
            engine.report['diagnostics'].extend(dict(kind='money_share', sector=kind, balance=balance,
                share=money_shares[kind]) for kind, balance in balances.items())
        engine.persist_investment_state()
        engine.meta['rng_state'] = engine.rng.getstate()
        engine.validate(before)
        # Числовые histories и diagnostics-строки месяца уходят в синхронный
        # буфер теми же объектами, что уже лежат в отчёте и на счетах: буфер
        # только фильтрует и батчит, форматирует и пишет в фон writer.
        _record_report_channels(engine, detail)
        if food_trace:
            engine.report['food_diagnostics'] = food_trace.finish(engine)
        if detail == 'deep':
            engine.build_causal_events()
        elif detail in ('standard', 'none'):
            engine.report['market_boards'] = []
            engine.report['events'] = []
            for order in engine.report['orders']:
                order.pop('match_observation', None)
        if investment_mode == 'async_snapshot_batch':
            from shadow.investment_batch import schedule_async_investment
            schedule_async_investment(engine, async_snapshot)
        timings['report'] = time.perf_counter()-phase_started
        timings['wall'] = time.perf_counter()-started
        engine.phase_timings = timings
        completed = True
    finally:
        if world is not None:
            world.end_step(engine, completed=completed)
    return engine.report


def _record_report_channels(engine, detail):
    """Отдать готовые числовые записи месяца синхронному буферу событий.

    Буфер — поток вывода, а не состояние: он получает ссылки на уже созданные
    строки отчёта и складских историй, ставит их в порядок и батчит. Ни одна
    строка здесь не форматируется и ни один файл не открывается. Списки не
    копируются, поэтому ``report``/``sales_history`` остаются единственным
    источником истины, а RNG, порядок сделок и порядок событий не меняются.
    """
    recorder = getattr(engine, 'event_recorder', None)
    if recorder is None:
        return 0
    from .event_buffer_boundary import DIAGNOSTICS, HISTORY, REPLAY
    written = recorder.record_rows(engine.report.get('diagnostics', ()),
                                   kind=DIAGNOSTICS, channel=DIAGNOSTICS)
    histories = []
    for enterprise in engine.enterprises.values():
        history = enterprise.get('sales_history')
        if history:
            histories.append(history[-1])
    for account in engine.accounts.values():
        for rows in account.get('stock_sales_history', {}).values():
            if rows:
                histories.append(rows[-1])
    written += recorder.record_rows(histories, kind=REPLAY, channel=HISTORY)
    return written


def load_monthly_driver():
    """Вернуть native-модуль monthly_driver или None, если он не собран."""
    try:
        return importlib.import_module('.monthly_driver', __package__)
    except (ImportError, ModuleNotFoundError):
        return None


def require_native_monthly():
    """Потребовать собранный драйвер там, где запрошен режим ``cython``.

    Молчаливый переход на Python-эталон в этом режиме запрещён: вызывающий
    просил native-оркестрацию, и подмена скрыла бы как раз то, что он просил.
    Ошибка возникает до мутации пролога месяца.
    """
    if _driver is None:
        raise RuntimeError(
            'Cython-расширение monthly_driver не собрано. Выполните: '
            'cd modules/economy/native && python setup.py build_ext --inplace')


NATIVE_MONTHLY_DRIVER = False

_driver = load_monthly_driver()
if _driver is not None:
    monthly_step = _driver.monthly_step
    NATIVE_MONTHLY_DRIVER = True
else:
    monthly_step = monthly_step_python
