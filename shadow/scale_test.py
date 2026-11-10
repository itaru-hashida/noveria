"""Масштабный замер: как растёт стоимость плана по числу агентов и workers.

Синтетическое размножение фирм поверх настоящего мира нужно, чтобы увидеть
форму роста до постройки генератора миров. Публичный рынок один и тот же,
поэтому это замер стоимости расчёта, а не экономики.
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
from shadow import agent_snapshot  # noqa: E402
from shadow.runner import plan_batch, budget_cap, prepare_market  # noqa: E402
from shadow.snapshot_store import SharedSnapshot  # noqa: E402


def measure(engine, repeat, workers, parameters):
    budget = budget_cap(engine)
    arrays, meta = agent_snapshot.build(engine, budget, repeat=repeat)
    layout, size = agent_snapshot.describe(arrays)
    snapshot = SharedSnapshot.create(arrays, layout, size)

    started = time.perf_counter()
    count = 0
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(plan_batch, (snapshot.name, layout, [agent], parameters))
                   for agent in range(meta['n_agents'])]
        for future in futures:
            count += sum(len(intents) for _, intents in future.result(timeout=600))
    elapsed = time.perf_counter()-started
    snapshot.release()
    return meta['n_agents'], count, elapsed, size


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int, default=73129)
    parser.add_argument('--months', type=int, default=3)
    parser.add_argument('--repeats', default='1,4,8,20')
    parser.add_argument('--workers', default='1,2,4,8')
    args = parser.parse_args()

    work = ROOT / 'tmp' / ('scale-%d' % args.seed)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    db, service = sr.build_world(args.seed, work, report_detail='none')
    for month in range(args.months):
        service.advance_turn(command_id='scale-%d-%d' % (args.seed, month), source='audit')
    engine = service.economy_state().engine
    prepare_market(engine)
    parameters = (engine.cfg['transport_rate'], engine.cfg['wage'],
                  engine.cfg['consumption_tax'], engine.cfg['markup'],
                  engine.cfg.get('enterprise_overhead_rate', 0.0))

    print('PARAMS: seed=%d months=%d repeats=%s workers=%s'
          % (args.seed, args.months, args.repeats, args.workers))
    print('%-8s %-8s %-10s %-10s %-10s %s'
          % ('agents', 'workers', 'seconds', 'ms/agent', 'speedup', 'intents'))
    base = None
    for repeat in (int(value) for value in args.repeats.split(',')):
        serial = None
        for workers in (int(value) for value in args.workers.split(',')):
            agents, intents, elapsed, size = measure(engine, repeat, workers, parameters)
            if workers == 1:
                serial = elapsed
                base = elapsed
            speedup = (serial/elapsed) if serial else 0.0
            print('%-8d %-8d %-10.3f %-10.2f %-10.2f %d'
                  % (agents, workers, elapsed, 1000*elapsed/agents, speedup, intents))
        print()
    db.close()
    shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    main()
