"""Масштабирование с ТЁПЛЫМ пулом: старт процессов исключён из замера.

Холодный пул стоит ~0,5-1,0 s на spawn и импорт numpy, что на месяце в 1 s
съедает весь выигрыш. В игре пул переживает месяцы, поэтому честная цифра —
второй и последующие раунды на том же пуле.
"""
import argparse
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
from shadow.runner import budget_cap, prepare_market  # noqa: E402
from shadow.snapshot_store import SharedSnapshot, read_arrays  # noqa: E402


def plan_batch(job):
    name, layout, agents, parameters = job
    shm, arrays = read_arrays(name, layout)
    try:
        return sum(len(agent_planner.plan(arrays, agent, *parameters)) for agent in agents)
    finally:
        shm.close()


def chunks(values, size):
    return [values[index:index+size] for index in range(0, len(values), size)]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int, default=73129)
    parser.add_argument('--months', type=int, default=3)
    parser.add_argument('--repeats', default='1,4,8,20')
    parser.add_argument('--workers', default='1,2,4,8')
    parser.add_argument('--batch', type=int, default=32)
    parser.add_argument('--rounds', type=int, default=3)
    args = parser.parse_args()

    work = ROOT / 'tmp' / ('warm-%d' % args.seed)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    db, service = sr.build_world(args.seed, work, report_detail='none')
    for month in range(args.months):
        service.advance_turn(command_id='warm-%d-%d' % (args.seed, month), source='audit')
    engine = service.economy_state().engine
    prepare_market(engine)
    parameters = (engine.cfg['transport_rate'], engine.cfg['wage'],
                  engine.cfg['consumption_tax'], engine.cfg['markup'],
                  engine.cfg.get('enterprise_overhead_rate', 0.0))

    print('PARAMS: seed=%d months=%d repeats=%s workers=%s batch=%d rounds=%d (warm pool)'
          % (args.seed, args.months, args.repeats, args.workers, args.batch, args.rounds))
    print('%-8s %-8s %-10s %-10s %-10s %s'
          % ('agents', 'workers', 'seconds', 'ms/agent', 'speedup', 'intents'))
    for repeat in (int(value) for value in args.repeats.split(',')):
        budget = budget_cap(engine)
        arrays, meta = agent_snapshot.build(engine, budget, repeat=repeat)
        layout, size = agent_snapshot.describe(arrays)
        snapshot = SharedSnapshot.create(arrays, layout, size)
        agents = list(range(meta['n_agents']))
        groups = chunks(agents, args.batch)
        serial = None
        for workers in (int(value) for value in args.workers.split(',')):
            best, intents = None, 0
            with ProcessPoolExecutor(max_workers=workers) as pool:
                for round_index in range(args.rounds):
                    started = time.perf_counter()
                    futures = [pool.submit(plan_batch, (snapshot.name, layout, group, parameters))
                               for group in groups]
                    intents = sum(future.result(timeout=600) for future in futures)
                    elapsed = time.perf_counter()-started
                    # Первый раунд включает холодный старт, берём минимум.
                    best = elapsed if best is None else min(best, elapsed)
            if workers == 1:
                serial = best
            print('%-8d %-8d %-10.3f %-10.2f %-10.2f %d'
                  % (meta['n_agents'], workers, best, 1000*best/meta['n_agents'],
                     (serial/best) if serial else 1.0, intents))
        snapshot.release()
        print()
    db.close()
    shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    main()
