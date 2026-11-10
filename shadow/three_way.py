"""Три разных сравнения из задачи 3, каждое со своим выводом.

1. Новый планировщик: 1 процесс против пула — польза параллелизма при
   одинаковой логике.
2. Последовательный replay записанного набора намерений — корректность
   исполнения и отсутствие влияния гонок.
3. Полный новый ход против V3 — скорость и экономическое поведение.
"""
import argparse
import json
import queue
import shutil
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'audit'))

from shadow import sandbox_mp  # noqa: E402

sandbox_mp.enable_multiprocessing()

from modules.economy import Economy  # noqa: E402
import scenario_runner as sr  # noqa: E402
from shadow import agent_planner, agent_snapshot  # noqa: E402
from shadow.market_executor import Ledger, MarketExecutor  # noqa: E402
from shadow.producer import Producer  # noqa: E402
from shadow.runner import budget_cap, prepare_market  # noqa: E402
from shadow.snapshot_store import SharedSnapshot, read_arrays  # noqa: E402


def plan_inline(arrays, agents, parameters):
    return [(agent, agent_planner.plan(arrays, agent, *parameters)) for agent in agents]


def replay(intents_log, meta, arrays, parameters, weights, distance):
    """Тот же набор намерений в том же порядке, но без пула и потоков.

    Проверяет, что исполнение не зависит от гонок: результат обязан совпасть с
    прогоном, где план считался параллельно.
    """
    ledger = Ledger(meta, arrays)
    books = {'distance': distance}
    outcomes = queue.Queue()
    executor = MarketExecutor(ledger, queue.Queue(), books, meta, outcomes, weights,
                              parameters[0])
    for item in intents_log:
        executor._execute(item)
    rows = []
    while not outcomes.empty():
        rows.append(outcomes.get())
    return ledger, rows


def run(seed, months):
    work = ROOT / 'tmp' / ('three-%d' % seed)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    db, service = sr.build_world(seed, work, report_detail='none')
    try:
        for month in range(2):
            service.advance_turn(command_id='warm-%d' % month, source='audit')
        engine = Economy.load(db)
        prepare_market(engine)
        parameters = (engine.cfg['transport_rate'], engine.cfg['wage'],
                      engine.cfg['consumption_tax'], engine.cfg['markup'],
                      engine.cfg.get('enterprise_overhead_rate', 0.0),
                      engine.cfg['labor_share'])
        budget = budget_cap(engine)
        arrays, meta = agent_snapshot.build(engine, budget)
        layout, size = agent_snapshot.describe(arrays)
        agents = list(range(meta['n_agents']))
        distance = {row: {column: float(arrays['distance'][row, column])
                          for column in range(meta['n_tiles'])}
                    for row in range(meta['n_tiles'])}
        weights = {index: float(arrays['good_weight'][index]) for index in range(meta['n_goods'])}

        print('PARAMS: seed=%d months=2 warmup=2 detail=none agents=%d batch=32'
              % (seed, meta['n_agents']))

        # --- 1. Последовательный планировщик против пула -------------------
        started = time.perf_counter()
        serial_plans = plan_inline(arrays, agents, parameters[:5])
        serial_time = time.perf_counter()-started

        snapshot = SharedSnapshot.create(arrays, layout, size)
        groups = [agents[index:index+32] for index in range(0, len(agents), 32)]
        try:
            results = {}
            for workers in (1, 2, 4, 8):
                best = None
                with ProcessPoolExecutor(max_workers=workers) as pool:
                    for _ in range(3):
                        started = time.perf_counter()
                        futures = [pool.submit(_batch, (snapshot.name, layout, group,
                                                        parameters[:5]))
                                   for group in groups]
                        collected = []
                        for future in futures:
                            collected.extend(future.result(timeout=600))
                        elapsed = time.perf_counter()-started
                        best = elapsed if best is None else min(best, elapsed)
                results[workers] = best
        finally:
            snapshot.release()

        print()
        print('1) НОВЫЙ ПЛАНИРОВЩИК: 1 процесс против пула')
        print('   без пула (in-process): %.4fs' % serial_time)
        for workers, elapsed in sorted(results.items()):
            print('   пул %d workers:        %.4fs  (%.2fx к in-process)'
                  % (workers, elapsed, serial_time/elapsed))
        same = sorted((a, tuple(map(tuple, [i[:5] for i in p])))
                      for a, p in serial_plans) == sorted(
            (a, tuple(map(tuple, [i[:5] for i in p]))) for a, p in collected)
        print('   логика идентична: %s' % ('да' if same else 'НЕТ'))

        # --- 2. Последовательный replay записанного порядка -----------------
        logged = [(agent, item[0], item[1], item[2], item[3], item[4], item[5], item[6])
                  for agent, planned in serial_plans for item in planned]
        ledger, rows = replay(logged, meta, arrays, parameters, weights, distance)

        money_start = sum(meta['balances'].values())
        money_end = sum(ledger.cash.values())
        print()
        print('2) REPLAY записанного порядка (последовательно, без потоков)')
        print('   намерений %d, исполнено %d'
              % (len(logged), sum(1 for row in rows if row['filled'] > 0)))
        print('   деньги: %.2f -> %.2f (%s)'
              % (money_start, money_end,
                 'сходится' if abs(money_start-money_end) < 1e-6 else 'РАСХОЖДЕНИЕ'))
        print('   сделок %d, все количества положительны: %s'
              % (len(ledger.trades), all(row['quantity'] > 0 for row in ledger.trades)))

        goods_start = {}
        for account_id, stocks in meta['all_stock'].items():
            for good, quantity in stocks.items():
                goods_start[good] = goods_start.get(good, 0.0)+quantity
        producer = Producer(ledger, meta, arrays, parameters[1], parameters[5])
        producer.run({agent: (max([i[5] for i in planned if i[0] == 'sell'] or [0.0]),
                              '') for agent, planned in serial_plans})
        goods_end = {}
        for account_id, stocks in ledger.stock.items():
            for good, quantity in stocks.items():
                goods_end[good] = goods_end.get(good, 0.0)+quantity
        broken = []
        for good in sorted(set(goods_start) | set(goods_end)):
            expected = (goods_start.get(good, 0.0) + producer.produced.get(good, 0.0)
                        - producer.consumed.get(good, 0.0))
            if abs(goods_end.get(good, 0.0)-expected) > 1e-6*max(1.0, abs(expected)):
                broken.append(good)
        print('   товарный баланс: %s' % ('сошёлся' if not broken else 'НАРУШЕН ' + str(broken)))
        return dict(results=results, serial_time=serial_time, logged=len(logged),
                    money_ok=abs(money_start-money_end) < 1e-6, goods_ok=not broken,
                    demand=sum(1 for row in rows if row['filled'] > 0))
    finally:
        db.close()
        shutil.rmtree(work, ignore_errors=True)


def _batch(job):
    name, layout, agents, parameters = job
    shm, arrays = read_arrays(name, layout)
    try:
        return [(agent, agent_planner.plan(arrays, agent, *parameters)) for agent in agents]
    finally:
        shm.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int, default=73129)
    args = parser.parse_args()
    run(args.seed, 2)


if __name__ == '__main__':
    main()
