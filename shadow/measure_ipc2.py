"""Почему пул медленнее: план дешёвый, а ожидание пачки — дорогое.

Считает на одном месяце:

    план всех агентов в процессе      — нижняя граница
    тот же план через пул             — с накладными
    одна пачка туда-обратно            — стоимость доставки
    простой исполнителя в ожидании пачки

Запуск:
    python -m shadow.measure_ipc2 --seed 73129 --months 8 --warmup 2 --workers 4
"""
import argparse
import shutil
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'audit'))

from shadow import sandbox_mp  # noqa: E402

sandbox_mp.enable_multiprocessing()

from audit.scenario_runner import build_world  # noqa: E402
from modules.economy import Economy  # noqa: E402
from shadow import agent_planner, agent_snapshot  # noqa: E402
from shadow.snapshot_store import SharedSnapshot, read_arrays  # noqa: E402
from shadow.turn import AgentTurn, plan_batch  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int, default=73129)
    parser.add_argument('--months', type=int, default=8)
    parser.add_argument('--warmup', type=int, default=2)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--batch', type=int, default=32)
    args = parser.parse_args()

    print('PARAMS: seed=%d months=%d warmup=%d workers=%d batch=%d detail=none'
          % (args.seed, args.months, args.warmup, args.workers, args.batch))

    work = ROOT / 'tmp' / ('mp2-%d' % args.seed)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    db, service = build_world(args.seed, work, report_detail='none')
    try:
        for month in range(args.warmup+args.months):
            service.advance_turn(command_id='mp2-%d' % month, source='audit')
        engine = Economy.load(db)
        engine.update_prices()
        engine.prepare_recovery_quotes()
        sells = engine.sell_orders()
        budget = {f['id']: engine.free_cash(f['id']) for f in engine.enterprises.values()}
        arrays, meta = agent_snapshot.build(engine, budget, sells=sells)
        parameters = tuple(engine.cfg[k] for k in
                           ('transport_rate', 'wage', 'consumption_tax', 'markup',
                            'enterprise_overhead_rate'))
        agents = list(range(meta['n_agents']))
        groups = [agents[i:i+args.batch] for i in range(0, len(agents), args.batch)]
        layout, size = agent_snapshot.describe(arrays)

        # 1. Нижняя граница: тот же план в процессе.
        best = None
        for _ in range(5):
            started = time.perf_counter()
            for agent in agents:
                agent_planner.plan(arrays, agent, *parameters)
            elapsed = time.perf_counter()-started
            best = elapsed if best is None else min(best, elapsed)
        in_process = best
        print()
        print('ПЛАН (нижняя граница):')
        print('  все %d агентов в процессе      %.4f s' % (len(agents), in_process))
        print('  на агента                       %.6f s' % (in_process/len(agents)))

        # 2. Тот же план через пул, раздельно: снимок и доставка.
        snapshot = SharedSnapshot.create(arrays, layout, size)
        try:
            best_total = None
            best_single = None
            with ProcessPoolExecutor(max_workers=args.workers) as pool:
                # прогрев: пул создан до измерений, worker-ы уже поднимались.
                list(pool.map(plan_batch, [(snapshot.name, layout, group, parameters)
                                           for group in groups]))
                for _ in range(3):
                    started = time.perf_counter()
                    list(pool.map(plan_batch, [(snapshot.name, layout, group, parameters)
                                               for group in groups]))
                    elapsed = time.perf_counter()-started
                    best_total = elapsed if best_total is None else min(best_total, elapsed)
                for _ in range(5):
                    started = time.perf_counter()
                    pool.submit(plan_batch, (snapshot.name, layout, groups[0],
                                             parameters)).result()
                    elapsed = time.perf_counter()-started
                    best_single = elapsed if best_single is None else min(best_single, elapsed)
        finally:
            snapshot.release()
        print()
        print('ЧЕРЕЗ ПУЛ:')
        print('  все пачки (%d)                  %.4f s' % (len(groups), best_total))
        print('  одна пачка туда-обратно         %.6f s' % best_single)
        print('  накладные на %d пачек            %.4f s'
              % (len(groups), best_total-in_process))
        print('  ускорение плана                 %.2fx' % (in_process/best_total))

        # 3. Сколько стоит ожидание одной пачки при 4 worker-ах.
        print()
        print('  пачек на worker: %.1f -> простой исполнителя, пока пачка считается'
              % (len(groups)/args.workers))
        # Второй месяц: измерить простой исполнителя в реальном ходе.
        rows = []
        for month in range(3):
            engine = Economy.load(db)
            planner = AgentTurn(pool=None, batch=args.batch)
            engine.step(enterprise_planner=planner)
            rows.append(dict(stream=dict(planner.metrics), wall=0.0))
            service.advance_turn(command_id='mp2-live-%d' % month, source='audit')
        exec_times = [row['stream'].get('executor_processing', 0.0) for row in rows]
        print()
        print('INLINE (без пула), 3 месяца:')
        print('  исполнитель %.4f s, планирование %.4f s'
              % (statistics.mean(exec_times),
                 statistics.mean(row['stream'].get('planning_delivery_inclusive') or 0.0
                                 for row in rows)))
    finally:
        db.close()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    main()
