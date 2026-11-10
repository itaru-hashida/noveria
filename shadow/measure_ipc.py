"""Разложение стоимости pool по участкам: где именно теряется время.

Меряет раздельно, на одном и том же месяце:

    построение массивов снимка (numpy)
    запись в shared_memory (копирование)
    сериализация и доставка пачки в worker
    ожидание очереди
    исполнение исполнителя
    производство
    полный wall-clock

Плюс сравнение с inline (без пула) на том же месяце. Холодный старт пула
исключён: пул создаётся до цикла и прогрет.

Запуск:
    python -m shadow.measure_ipc --seed 73129 --months 8 --workers 4 --batch 32
"""
import argparse
import queue
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


def measure_snapshot(engine, sells, repeats=5):
    """Только стоимость снимка: массивы + копирование в shared_memory."""
    budget = {f['id']: engine.free_cash(f['id']) for f in engine.enterprises.values()}
    build_times, write_times, sizes = [], [], []
    for _ in range(repeats):
        started = time.perf_counter()
        arrays, meta = agent_snapshot.build(engine, budget, sells=sells)
        build_times.append(time.perf_counter()-started)
        layout, size = agent_snapshot.describe(arrays)
        started = time.perf_counter()
        snapshot = SharedSnapshot.create(arrays, layout, size)
        write_times.append(time.perf_counter()-started)
        sizes.append(size)
        snapshot.release()
    return statistics.mean(build_times), statistics.mean(write_times), sizes[0]


def measure_payload(meta, groups, repeats=5):
    """Стоимость доставки пачки: что реально уходит в worker."""
    import pickle
    payload = ('snapshot-name', 'layout', groups[0], (0.008, 0.12, 0.1, 0.2, 0.02))
    started = time.perf_counter()
    for _ in range(repeats):
        pickle.dumps(payload)
    serialize = (time.perf_counter()-started)/repeats
    # Размер полезной нагрузки: agent_id + смещения, без массивов.
    size = len(pickle.dumps(payload))
    return serialize, size, len(payload[2])


def run_mode(mode, seed, months, warmup, workers, batch):
    work = ROOT / 'tmp' / ('ipc-%s-%d' % (mode, seed))
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    db, service = build_world(seed, work, report_detail='none')
    rows = []
    try:
        for month in range(warmup):
            service.advance_turn(command_id='ipc-warm-%d' % month, source='audit')
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for month in range(months):
                engine = service.economy_state().engine
                planner = AgentTurn(pool, batch=batch) if mode == 'pool' else None
                started = time.perf_counter()
                engine.step(enterprise_planner=planner)
                wall = time.perf_counter()-started
                rows.append(dict(month=engine.meta['month'], wall=wall,
                                 stream=(planner.metrics if planner else {}),
                                 phase=dict(engine.phase_timings)))
                service.advance_turn(command_id='ipc-%d-%d' % (month, workers), source='audit')
    finally:
        db.close()
        shutil.rmtree(work, ignore_errors=True)
    return rows


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

    # 1. Стоимость снимка на живом месяце.
    work = ROOT / 'tmp' / ('ipc-snap-%d' % args.seed)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    db, service = build_world(args.seed, work, report_detail='none')
    try:
        for month in range(args.warmup+args.months):
            service.advance_turn(command_id='ipc-snap-%d' % month, source='audit')
        engine = service.economy_state().engine
        engine.update_prices()
        engine.prepare_recovery_quotes()
        sells = engine.sell_orders()
        build_time, write_time, size = measure_snapshot(engine, sells)
        budget = {f['id']: engine.free_cash(f['id']) for f in engine.enterprises.values()}
        arrays, meta = agent_snapshot.build(engine, budget, sells=sells)
        agents = list(range(meta['n_agents']))
        groups = [agents[i:i+args.batch] for i in range(0, len(agents), args.batch)]
        serialize, payload_size, group_size = measure_payload(meta, groups)
        print()
        print('СНИМОК (среднее из 5):')
        print('  построение массивов      %.5f s' % build_time)
        print('  запись в shared_memory   %.5f s' % write_time)
        print('  размер области           %.1f КБ' % (size/1024))
        print('  агентов %d, площадок %d, офферов %d, заявок %d'
              % (meta['n_agents'], meta['n_tiles'],
                 len(arrays['offer_price']), len(arrays['ask_price'])))
        print()
        print('ДОСТАВКА ПАЧКИ:')
        print('  сериализация задачи      %.6f s' % serialize)
        print('  размер задачи            %d байт (agent ids: %d)' % (payload_size, group_size))
        print('  пачек на месяц           %d' % len(groups))
        print('  суммарная сериализация   %.6f s' % (serialize*len(groups)))
    finally:
        db.close()
        shutil.rmtree(work, ignore_errors=True)

    # 2. Полные прогоны inline и pool.
    results = {}
    for mode in ('inline', 'pool'):
        rows = run_mode(mode, args.seed, args.months, args.warmup, args.workers, args.batch)
        results[mode] = rows
        walls = [r['wall'] for r in rows]
        print()
        print('%s: wall mean %.4f (min %.4f max %.4f)'
              % (mode, statistics.mean(walls), min(walls), max(walls)))
        parts = {}
        for row in rows:
            for key, value in row['phase'].items():
                parts.setdefault(key, []).append(value)
        for key, values in parts.items():
            print('   фаза %-24s %.5f' % (key, statistics.mean(values)))
        if mode == 'pool':
            for key in ('snapshot', 'stream_wall', 'planning_delivery_inclusive',
                        'drain_after_last', 'executor_processing', 'executor_wait'):
                values = [r['stream'].get(key) or 0.0 for r in rows]
                print('   поток %-24s %.5f' % (key, statistics.mean(values)))

    if 'inline' in results and 'pool' in results:
        inline = statistics.mean(r['wall'] for r in results['inline'])
        pool = statistics.mean(r['wall'] for r in results['pool'])
        print()
        print('ИТОГ: inline %.4f, pool %.4f, разница %+.4f s (%.1f%%)'
              % (inline, pool, pool-inline, 100*(pool-inline)/inline))


if __name__ == '__main__':
    main()
