"""Масштабный замер: агент на задачу против пачки агентов на задачу.

Гипотеза: план одного агента стоит ~0,4 ms, а round-trip задачи в пул —
сотни микросекунд. Тогда параллельность по одному агенту бесполезна, и
выигрыш появится только при объединении агентов в пачки.
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
    """План пачки агентов: один round-trip на многих агентов."""
    name, layout, agents, parameters = job
    shm, arrays = read_arrays(name, layout)
    try:
        return [(agent, agent_planner.plan(arrays, agent, *parameters))
                for agent in agents]
    finally:
        shm.close()


def chunks(values, size):
    return [values[index:index+size] for index in range(0, len(values), size)]


def measure(engine, repeat, workers, parameters, batch):
    budget = budget_cap(engine)
    arrays, meta = agent_snapshot.build(engine, budget, repeat=repeat)
    layout, size = agent_snapshot.describe(arrays)
    snapshot = SharedSnapshot.create(arrays, layout, size)

    agents = list(range(meta['n_agents']))
    groups = [agents] if batch == 0 else chunks(agents, batch)
    started = time.perf_counter()
    intents = 0
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(plan_batch, (snapshot.name, layout, group, parameters))
                   for group in groups]
        for future in futures:
            for _agent, planned in future.result(timeout=600):
                intents += len(planned)
    elapsed = time.perf_counter()-started
    snapshot.release()
    return meta['n_agents'], len(groups), intents, elapsed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int, default=73129)
    parser.add_argument('--months', type=int, default=3)
    parser.add_argument('--repeats', default='1,20')
    parser.add_argument('--workers', default='1,2,4,8')
    parser.add_argument('--batches', default='1,32,256,0', help='0 = все агенты одной задачей')
    args = parser.parse_args()

    work = ROOT / 'tmp' / ('batch-%d' % args.seed)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    db, service = sr.build_world(args.seed, work, report_detail='none')
    for month in range(args.months):
        service.advance_turn(command_id='batch-%d-%d' % (args.seed, month), source='audit')
    engine = service.economy_state().engine
    prepare_market(engine)
    parameters = (engine.cfg['transport_rate'], engine.cfg['wage'],
                  engine.cfg['consumption_tax'], engine.cfg['markup'],
                  engine.cfg.get('enterprise_overhead_rate', 0.0))

    print('PARAMS: seed=%d months=%d repeats=%s batches=%s workers=%s'
          % (args.seed, args.months, args.repeats, args.batches, args.workers))
    print('%-8s %-8s %-8s %-9s %-9s %s'
          % ('agents', 'batch', 'workers', 'tasks', 'seconds', 'speedup'))
    for repeat in (int(value) for value in args.repeats.split(',')):
        for batch in (int(value) for value in args.batches.split(',')):
            serial = None
            for workers in (int(value) for value in args.workers.split(',')):
                agents, tasks, intents, elapsed = measure(engine, repeat, workers, parameters, batch)
                if workers == 1:
                    serial = elapsed
                print('%-8d %-8d %-8d %-9d %-9.3f %.2fx'
                      % (agents, batch if batch else agents, workers, tasks, elapsed,
                         (serial/elapsed) if serial else 1.0))
            print()
    db.close()
    shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    main()
