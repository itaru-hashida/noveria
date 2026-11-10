"""Почему лесорубы недопроизводят: ограничение выпуска и труд провинции.

Сравнивает V3 и pool по товару wood: план, выпуск, названное ограничение,
свободный труд в провинциях и сколько дерева закупают потребители.
"""
import argparse
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'audit'))

from shadow import sandbox_mp  # noqa: E402

sandbox_mp.enable_multiprocessing()

from concurrent.futures import ProcessPoolExecutor  # noqa: E402

from audit.scenario_runner import build_world  # noqa: E402
from modules.economy import Economy  # noqa: E402
from shadow.turn import AgentTurn  # noqa: E402


def run(mode, seed, warmup, months, workers):
    work = ROOT / 'tmp' / ('wood-%s-%d' % (mode, seed))
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    db, service = build_world(seed, work, report_detail='none')
    try:
        for month in range(warmup):
            service.advance_turn(command_id='wd-warm-%d' % month, source='audit')
        snapshot = None
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for month in range(months):
                engine = Economy.load(db)
                planner = AgentTurn(pool, batch=32) if mode == 'pool' else None
                engine.step(enterprise_planner=planner)
                if month == months-1:
                    snapshot = (engine, planner)
                service.advance_turn(command_id='wd-%d' % month, source='audit')
        return snapshot
    finally:
        db.close()
        shutil.rmtree(work, ignore_errors=True)


def describe(engine, planner, label):
    print('=== %s, месяц %d' % (label, engine.meta['month']))
    for good in ('wood', 'planks'):
        firms = [e for e in engine.enterprises.values()
                 if e['good'] == good and e['status'] != 'closed']
        planned = sum((f.get('production_plan') or {}).get('output', 0.0) for f in firms)
        output = sum(f.get('output', 0.0) for f in firms)
        capacity = sum(f['capacity'] for f in firms)
        limits = Counter(f.get('production_limit') or '-' for f in firms)
        print('  %-7s фирм %3d мощность %7.0f план %7.0f выпуск %7.0f | %s'
              % (good, len(firms), capacity, planned, output,
                 ', '.join('%s=%d' % i for i in limits.most_common(3))))
    labor = (engine.report.get('labor_available') or [])
    used = Counter()
    for firm in engine.enterprises.values():
        if firm['status'] == 'closed':
            continue
        used[(firm['state_id'], firm['province_id'])] += firm.get('workers', 0.0)
    free = []
    for row in labor:
        key = (row['state_id'], row['province_id'])
        free.append((row['workers']-used[key], key[1]))
    free.sort()
    print('  провинций с наименьшим свободным трудом: %s'
          % [(pid, round(value, 1)) for value, pid in free[:4]])
    print('  свободный труд всего %.0f, занято %.0f'
          % (sum(row['workers'] for row in labor), sum(used.values())))
    buys = Counter()
    for order in engine.report['orders']:
        if order['side'] == 'buy' and order['good'] == 'wood':
            buys[order['purpose']] += order['quantity']
    print('  спрос на wood: %s' % dict(buys))
    if planner is not None:
        traces = planner.traces
        wood_firms = [f for f in traces if engine.enterprises[f]['good'] == 'wood']
        for firm in wood_firms[:3]:
            print('  %s trace: %s' % (firm, traces[firm]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int, default=73129)
    parser.add_argument('--warmup', type=int, default=2)
    parser.add_argument('--months', type=int, default=36)
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()
    print('PARAMS: seed=%d warmup=%d months=%d workers=%d detail=none'
          % (args.seed, args.warmup, args.months, args.workers))
    engine, planner = run('v3', args.seed, args.warmup, args.months, args.workers)
    describe(engine, planner, 'V3')
    print()
    engine, planner = run('pool', args.seed, args.warmup, args.months, args.workers)
    describe(engine, planner, 'pool')


if __name__ == '__main__':
    main()
