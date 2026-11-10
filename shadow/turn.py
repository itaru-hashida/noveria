"""Полный экспериментальный месяц: чистый план, штатный учёт и сохранение V3.

Исполнитель находится в родителе: worker не получает живой движок. Результаты
пакетов поступают FIFO по готовности, внутри пакета сохраняется упаковка.
"""
import queue
import random
import time

from shadow import agent_planner, agent_snapshot
from shadow.snapshot_store import SharedSnapshot, read_arrays


def plan_detailed(arrays, agent, parameters):
    trace = {}
    intents = agent_planner.plan(arrays, agent, *parameters, trace=trace)
    return agent, intents, trace


def plan_batch(job):
    name, layout, agents, parameters = job
    memory, arrays = read_arrays(name, layout)
    try:
        return [plan_detailed(arrays, agent, parameters) for agent in agents]
    finally:
        memory.close()


class AgentTurn:
    def __init__(self, pool, batch=32, replay=None):
        if batch < 1:
            raise ValueError('batch должен быть положительным')
        self.pool = pool
        self.batch = batch
        self.replay = replay
        self.log = []
        self.metrics = {}
        self.traces = {}

    def __call__(self, engine, existing, sells):
        started = time.perf_counter()
        eligible = {e['id'] for e in existing if e['status'] != 'closed'
                    and not e.get('suspended') and e['available_from'] <= engine.meta['month']}
        # Финансирование владельца уже прошло обычное recover_enterprises.
        # Ожидаемые деньги владельца не являются кассой предприятия.
        budget = {}
        for firm in eligible:
            account = engine.accounts[firm]
            account['protected'] = max(0, account['protected']-account.pop('operating_protected', 0))
            budget[firm] = engine.free_cash(firm)
        arrays, meta = agent_snapshot.build(engine, budget, sells=sells)
        parameters = tuple(engine.cfg[k] for k in
                           ('transport_rate', 'wage', 'consumption_tax', 'markup', 'enterprise_overhead_rate'))
        agents = [i for i, firm in enumerate(meta['firms']) if firm in eligible]
        random.Random(engine.meta['month']).shuffle(agents)
        groups = [agents[i:i+self.batch] for i in range(0, len(agents), self.batch)]
        layout, size = agent_snapshot.describe(arrays)
        snapshot = SharedSnapshot.create(arrays, layout, size) if self.pool and self.replay is None else None
        self.metrics['snapshot'] = time.perf_counter()-started
        execution, waiting = 0.0, 0.0
        results = queue.Queue()
        futures = []

        def completed(future):
            try:
                results.put((future.result(), None, time.perf_counter()))
            except BaseException as exc:
                results.put((None, exc, time.perf_counter()))

        planning_started = time.perf_counter()
        last_ready = planning_started
        try:
            if self.replay is None and self.pool:
                futures = [self.pool.submit(plan_batch, (snapshot.name, layout, group, parameters))
                           for group in groups]
                for future in futures:
                    future.add_done_callback(completed)
            # Потребители сохраняют первый доступ к местным запасам. Исполнение
            # идёт пока workers считают на неизменяемом снимке до этих сделок.
            tick = time.perf_counter()
            initial = [o for o in engine.report['orders'] if o['side'] == 'buy']
            for order in initial:
                if order['purpose'] == 'Базовые нужды':
                    engine.match(order, sells, local_only=True)
            for order in initial:
                engine.match(order, sells)
            execution += time.perf_counter()-tick
            if self.replay is not None:
                batches = [self.replay]
            elif self.pool is None:
                batches = [[plan_detailed(arrays, agent, parameters)
                            for agent in group] for group in groups]
            else:
                batches = None
            for index in range(len(batches) if batches is not None else len(groups)):
                if batches is None:
                    tick = time.perf_counter()
                    batch, error, ready = results.get(timeout=600)
                    last_ready = max(last_ready, ready)
                    waiting += time.perf_counter()-tick
                    if error:
                        raise error
                else:
                    batch = batches[index]
                for agent, intents, trace in batch:
                    self.log.append((agent, intents, trace))
                    self.traces[meta['firms'][agent]] = trace
                    tick = time.perf_counter()
                    self.execute(engine, sells, meta, agent, intents, trace)
                    execution += time.perf_counter()-tick
        finally:
            # Ошибка worker не должна освобождать снимок, пока другой его читает.
            for future in futures:
                try:
                    future.result(timeout=600)
                except Exception:
                    pass
            if snapshot:
                snapshot.release()
        ended = time.perf_counter()
        self.metrics.update(stream_wall=ended-planning_started,
                            planning_delivery_inclusive=last_ready-planning_started if self.pool else None,
                            drain_after_last=ended-last_ready if self.pool else None,
                            executor_processing=execution, executor_wait=waiting,
                            agents=len(agents), intents=sum(len(p) for _, p, _ in self.log),
                            trades=len(engine.report['trades']), batch=self.batch)

    @staticmethod
    def execute(engine, sells, meta, agent, intents, trace):
        firm = engine.enterprises[meta['firms'][agent]]
        account = engine.accounts[firm['id']]
        output = intents[0][5]
        reason = intents[0][6]
        wage_reserve = output*engine.goods[firm['good']]['labor']*engine.cfg['wage']
        account['payroll_protected'] = wage_reserve
        account['protected'] += wage_reserve
        firm.update(agent_sales_forecast=trace['forecast'], sales_forecast=trace['forecast'],
                    reachable_demand=trace['observed'], planned_output=output,
                    input_target_output=output, production_limit=reason,
                    expected_unit_cost=trace['unit_cost'], expected_price=trace['price'],
                    expected_margin=trace['price']-trace['unit_cost'])
        firm['production_plan'] = dict(month=engine.meta['month'], output=output,
                                       sale_price=trace['price'], reason=reason)
        for side, good, quantity, price, purpose, _, _ in intents:
            # Склады уже опубликованы sell_orders ровно один раз до снимка.
            if side == 'sell':
                continue
            order = engine.order(firm['id'], meta['goods'][good], quantity, side, price, purpose)
            engine.match(order, sells)
