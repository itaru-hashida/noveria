"""Кто простаивает на длинном горизонте и какой вход дефицитен.

На 36 месяцах для pool и V3: распределение простоев по товару и причине,
дефицитные входы по цепочке, узкое место по каждому товару.

Запуск:
    python -m shadow.diag_idle --seed 73129 --months 36 --warmup 2 --workers 4
"""
import argparse
import shutil
import sys
from collections import Counter, defaultdict
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int, default=73129)
    parser.add_argument('--months', type=int, default=36)
    parser.add_argument('--warmup', type=int, default=2)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--modes', default='v3,pool')
    args = parser.parse_args()

    print('PARAMS: seed=%d months=%d warmup=%d workers=%d detail=none'
          % (args.seed, args.months, args.warmup, args.workers))

    for mode in args.modes.split(','):
        work = ROOT / 'tmp' / ('idle-%s-%d' % (mode, args.seed))
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        db, service = build_world(args.seed, work, report_detail='none')
        try:
            for month in range(args.warmup):
                service.advance_turn(command_id='il-warm-%d' % month, source='audit')
            with ProcessPoolExecutor(max_workers=args.workers) as pool:
                for month in range(args.months):
                    engine = Economy.load(db)
                    planner = AgentTurn(pool, batch=32) if mode == 'pool' else None
                    engine.step(enterprise_planner=planner)
                    if month >= args.months-2:
                        summarise(engine, mode)
                    service.advance_turn(command_id='il-%d' % month, source='audit')
        finally:
            db.close()
            shutil.rmtree(work, ignore_errors=True)


def summarise(engine, mode):
    print()
    print('=== %s, месяц %d' % (mode.upper(), engine.meta['month']))
    # Простои: причина × товар.
    table = defaultdict(Counter)
    for firm in engine.enterprises.values():
        if firm['status'] != 'idle':
            continue
        table[firm['good']][firm.get('reason') or '-'] += 1
    for good in sorted(table, key=lambda g: -sum(table[g].values())):
        total = sum(table[good].values())
        print('  %-8s простояло %2d: %s'
              % (good, total, ', '.join('%s=%d' % i for i in table[good].most_common(3))))
    # Узкое место по каждому товару: план против выпуска.
    print('  %-8s %-9s %-9s %-9s %-9s' % ('товар', 'мощность', 'план', 'выпуск', 'сделки'))
    trades = Counter()
    for trade in engine.report['trades']:
        trades[trade['good']] += trade['quantity']
    for good in sorted({e['good'] for e in engine.enterprises.values()}):
        firms = [e for e in engine.enterprises.values()
                 if e['good'] == good and e['status'] != 'closed']
        if not firms:
            continue
        capacity = sum(f['capacity'] for f in firms)
        planned = sum((f.get('production_plan') or {}).get('output', 0.0) for f in firms)
        output = sum(f.get('output', 0.0) for f in firms)
        print('  %-8s %-9.0f %-9.0f %-9.0f %-9.0f'
              % (good, capacity, planned, output, trades.get(good, 0.0)))
    # Дефицит входов: что закупали и сколько не получили.
    print('  недополученные входы (товар): спрос / получили')
    demand = defaultdict(lambda: [0.0, 0.0])
    for order in engine.report['orders']:
        if order['side'] != 'buy':
            continue
        demand[order['good']][0] += order['quantity']
        demand[order['good']][1] += order['quantity']-order['remaining']
    for good, (asked, got) in sorted(demand.items(), key=lambda x: -(x[1][0]-x[1][1])):
        if asked-got > 1.0:
            print('    %-8s %8.1f / %8.1f (недополучено %.1f)'
                  % (good, asked, got, asked-got))


if __name__ == '__main__':
    main()
