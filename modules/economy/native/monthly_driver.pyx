# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Месячный orchestration-цикл V3: одна точка входа вместо десятков переходов.

Python вызывает ``monthly_step(engine, ...)`` один раз за месяц. Всё, что
между этим вызовом и возвратом отчёта, — этот модуль: он последовательно
вызывает фазы месяца в том порядке, в котором они шли в ``Economy.step``.

Ядро не хранит экономику. Оно получает сам живой движок и работает с его
существующими хранилищами (``accounts``, ``report['orders']``, ``cash_store``,
``stock_store``, ``order_store``, ``world_state``), поэтому второго
``WorldState``, ledger'а или копии состояния здесь нет.

Границы Python→Cython, которые исчезли: фазы больше не дёргают друг друга
через Python-диспетчер ``Economy.step``; последовательность, тайминги,
условия ``planning_mode``/``investment_mode``/``native_mode`` и порядок
отчёта живут здесь.

Что осталось Python (осознанно, это не численные циклы):

  * ``shuffled`` — RNG месяца; порядок покупателей/товаров задаёт генератор;
  * ``world.begin_step``/``end_step`` — lifecycle единого контейнера;
  * ``update_prices``/``recover_enterprises``/``project_requests`` и прочие
    фазы — существующие native-границы со своими ядрами;
  * ``report``/``events``/``summarize`` — форматирование отчёта и хуки;
  * ``log_event``/``build_causal_events`` — слой причинных событий;
  * ``MonthlyHooks`` (``monthly_hooks``) — сброс и защита резервов месяца,
    истечение заявок и диагностика отчёта. Это поля API-слоя и отчёта, а не
    деньги: ``balance``/``reserved`` меняет только ``CashStore``, склад —
    ``StockStore``, поля заявок — ``OrderStore``.

Хуки месяца — единственный обратный переход Cython→Python внутри месяца, и
их не больше, чем участков, которые остались Python. Фасад ``Economy.step``
по-прежнему делает ровно один вызов на месяц.

Модуль не импортирует ``modules.economy.v3``: движок приходит аргументом,
поэтому циклического импорта нет. EPS и прочие константы тоже приходят
аргументами либо берутся у движка.
"""

import time


cdef object _PERF = time.perf_counter


cdef object _call(object fn, object engine):
    """Вызвать Python-хук фазы: явная граница там, где фаза осталась Python."""
    return fn(engine)


cdef object _make_hooks(object engine):
    """Взять хуки месяца у движка или собрать их поверх него.

    Хуки — это внешний API-слой, а не состояние: объект держит ссылку на
    живой движок. Приложение может подменить их (``engine.monthly_hooks``),
    и тогда ядро обязано звать именно их; иначе берётся ``MonthlyHooks``.
    """
    hooks = getattr(engine, 'monthly_hooks', None)
    if hooks is None:
        from .monthly_hooks import MonthlyHooks
        hooks = MonthlyHooks(engine)
    return hooks


def monthly_step(object engine, object eps, object planning_mode='v3',
                 object investment_mode='v3', object investment_workers=1,
                 object native_mode='off', object enterprise_planner=None):
    """Выполнить один месяц: все фазы в прежнем порядке и с прежним RNG.

    Аргументы повторяют ``Economy.step`` один в один, чтобы фасад оставался
    тонким: он проверяет режимы, готовит внешний контекст и делает ровно один
    вызов. Возвращается живой ``engine.report`` — тот же объект, что и раньше.
    """
    cdef object started
    cdef object timings
    cdef object phase_started
    cdef object before
    cdef object prior_cash
    cdef object existing
    cdef object world
    cdef bint completed = False
    cdef object lagged_plans = None
    cdef object lagged_firms = ()
    cdef object sells
    cdef object detail
    cdef object households
    cdef object food_trace = None
    cdef object buys
    cdef object planning_sells
    cdef object aggregates
    cdef object async_snapshot = None
    cdef object order
    cdef object hooks

    started = _PERF()
    timings = {}
    hooks = _make_hooks(engine)

    # --- пролог: месяц, отчёт, счета, склад -------------------------------
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
    # Месячный сброс резерва, защита кассы и резерв склада — Python-хук
    # ``MonthlyHooks.reset_month_reserves``. Хук не владеет деньгами: резерв
    # меняется через CashStore, склад — через StockStore, а защита кассы
    # остаётся полем представления. Порядок обхода прежний.
    hooks.reset_month_reserves(engine.accounts)
    if native_mode == 'cython':
        engine.stock_store.sync()
    existing = list(engine.enterprises.values())
    # Месячный numeric-сброс полей предприятий — в enterprise_reset_boundary:
    # ядро получает готовый список existing и пишет те же поля в том же
    # порядке ключей; RNG, report и порядок итерации остаются здесь.
    from .enterprise_reset_boundary import reset_enterprises
    reset_enterprises(existing)
    # Lifecycle единого WorldState. Контейнер не считает экономику: он
    # лишь привязывается к уже существующим хранилищам на время native-фаз.
    # В Python mode шаг не открывается и WorldState не создаётся: ленивое
    # свойство остаётся единственным способом его получить.
    world = engine.world_state if native_mode == 'cython' else None
    if world is not None:
        world.begin_step(engine)
    try:
        # --- подготовка рынка и заявок -----------------------------------
        if native_mode == 'cython':
            from .update_prices_boundary import run_native_public_projects, run_update_prices
            if planning_mode == 'lagged_batch':
                run_update_prices(engine)
            else:
                run_native_public_projects(engine)
        else:
            engine.update_prices()
        if planning_mode == 'lagged_batch':
            from shadow.lagged_batch import apply_plans, build_snapshot, plan_all, publish_sales
            phase_started = _PERF()
            snapshot = build_snapshot(engine)
            lagged_plans = plan_all(snapshot)
            lagged_firms = snapshot.eligible
            timings['lagged_snapshot_planning'] = _PERF()-phase_started
            engine.report['planning_mode'] = planning_mode
        elif native_mode != 'cython':
            engine.prepare_recovery_quotes()
        # В lagged_batch стартовые склады фирм публикуются их intents ровно один
        # раз; заявки остальных участников и сам sell_orders не меняются.
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
        # Строительные заявки реальны и обеспечены до расчёта потребностей в сырье.
        if investment_mode == 'snapshot_batch':
            from shadow.investment_batch import run_snapshot_batch
            investment_timings = run_snapshot_batch(engine, sells, workers=investment_workers)
            timings.update(investment_timings)
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
        timings['preparation'] = _PERF()-started

        # --- планирование предприятий -------------------------------------
        phase_started = _PERF()
        if planning_mode == 'lagged_batch':
            apply_plans(engine, lagged_plans)
            timings['lagged_apply'] = _PERF()-phase_started
        elif enterprise_planner is None:
            engine.plan_enterprises(existing, sells)
        else:
            enterprise_planner(engine, existing, sells)
        timings['planning_execution'] = _PERF()-phase_started

        # --- рынок: матчинг, допкупка, итоги ------------------------------
        phase_started = _PERF()
        buys = [o for o in engine.report['orders'] if o['side'] == 'buy']
        # Местная базовая потребность получает первый доступ к местному офферу.
        # Второй общий проход сохраняет импорт, экспорт и обычную конкуренцию
        # за весь невыкупленный остаток. Это правило применимо к любому товару
        # из ``basic_needs``, а не к отдельно названному хлебу.
        for buy in engine.shuffled([o for o in buys if o['purpose'] == 'Базовые нужды']):
            engine.match(buy, sells, local_only=True)
        # Внешний цикл покупателей — в Cython. Список уже перемешан здесь,
        # поэтому порядок и расход RNG остаются прежними; книга, наблюдения и
        # финализация вызываются как хуки движка, а обход книги выполняет
        # существующий внутренний цикл.
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
        # Снимок спроса сделан до поздних доходов; остатки заявок больше не блокируют кассу.
        # Истечение заявок — Python-хук: статус меняет OrderStore, поля
        # execution_status/reason и reserved_at_clearing остаются в отчёте.
        hooks.expire_orders()
        timings['market_tail'] = _PERF()-phase_started

        # --- производство, потребление, расчёты ---------------------------
        phase_started = _PERF()
        engine.reserve_expedited_construction_labor()
        engine.produce(existing, planning_sells)
        engine.advance_public_projects()
        engine.finish_projects()
        engine.consume_owner_purchases()
        # Числовой цикл потребления домохозяйств вынесен в
        # consumption_boundary: она держит эталонную Python-ветку и
        # fallback без собранного .pyd. Порядок домохозяйств, порядок
        # товаров, списание через существующую stock boundary и все
        # side effects (need, report) остаются за Python-хуками границы.
        from . import consumption_boundary as _consumption
        _consumption.consume_households(engine, households)
        # История продаж собирается одним проходом по книгам и сделкам. Раньше
        # для каждой фирмы заново перебирались все заявки месяца, а для каждого
        # слота склада — все сделки: 1,7 млн итераций за ход при 230 фирмах.
        # Слагаемые по-прежнему складываются встроенным sum: в Python 3.12 он
        # суммирует float с компенсацией погрешности, и простое накопление в
        # словаре дало бы расхождение в последнем знаке, а оно меняет траекторию.
        # Числовые writers истории вынесены в report_history_boundary: она же
        # держит Python-эталон и fallback без собранного .pyd. Порядок заявок,
        # порядок предприятий и счетов и живой report остаются прежними.
        from . import report_history_boundary as _report_history
        _report_history.update_sales_history(engine, eps)
        _report_history.update_stock_sales_history(engine)
        # Защита кассы под проект и резерв под проекты — тоже хуки: это поля
        # представления, а расчёт резерва остаётся за ``project_cash_reserve``.
        hooks.protect_balances(engine.accounts)
        engine.settle(list(engine.enterprises.values()), households)
        engine.pay_dividends(planning_sells)
        engine.update_demography()
        engine.apply_migration()
        hooks.apply_project_reserves(engine.accounts, planning_sells)
        hooks.extend_diagnostics(detail)
        timings['production_settlement'] = _PERF()-phase_started

        # --- отчёт, инварианты, события -----------------------------------
        phase_started = _PERF()
        if detail == 'none':
            engine.update_welfare()
        else:
            engine.summarize(prior_cash)
            # Числовые агрегаты отчёта (account_flows, sector_flows, balances,
            # money_shares, stocks, active/open) считает report_history_
            # boundary: она же держит Python-эталон и fallback без .pyd.
            # money_share-строки diagnostics — форматирование отчёта,
            # остаются здесь, из возвращённых чисел.
            balances, money_shares = _report_history.build_report_aggregates(engine, engine.report)
            engine.report['diagnostics'].extend(dict(kind='money_share', sector=kind, balance=balance,
                share=money_shares[kind]) for kind, balance in balances.items())
        engine.persist_investment_state()
        engine.meta['rng_state'] = engine.rng.getstate()
        engine.validate(before)
        if food_trace:
            engine.report['food_diagnostics'] = food_trace.finish(engine)
        if detail == 'deep':
            engine.build_causal_events()
        elif detail in ('standard', 'none'):
            # Стандартный отчёт хранит результаты рынка, но не тяжёлый снимок
            # каждой книги и не дублирующий слой причинных событий.
            engine.report['market_boards'] = []
            engine.report['events'] = []
            for order in engine.report['orders']:
                order.pop('match_observation', None)
        if investment_mode == 'async_snapshot_batch':
            from shadow.investment_batch import schedule_async_investment
            schedule_async_investment(engine, async_snapshot)
        timings['report'] = _PERF()-phase_started
        timings['wall'] = _PERF()-started
        engine.phase_timings = timings
        completed = True
    finally:
        # Шаг закрывается и при исключении: контейнер обязан снять флаг и
        # сохранить сводку, иначе следующий begin_step упрётся в открытый
        # шаг. Значения игры здесь не трогаются, отката нет.
        if world is not None:
            world.end_step(engine, completed=completed)
    return engine.report
