"""Почему закупка входа ограничена: кривая офферов против предела и кассы.

Для пекарен в одном месяце показывает по каждому входу: сколько нужно,
сколько доступно на рынке, сколько affordable по пределу маржи, сколько
куплено и по какой цене. Плюс rejected orders по причине
landed_price_above_limit.
"""
import argparse
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
from shadow import agent_planner, agent_snapshot  # noqa: E402
from shadow.turn import AgentTurn  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int, default=73129)
    parser.add_argument('--months', type=int, default=12)
    parser.add_argument('--warmup', type=int, default=2)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--good', default='bread')
    args = parser.parse_args()

    print('PARAMS: seed=%d months=%d warmup=%d workers=%d detail=none good=%s'
          % (args.seed, args.months, args.warmup, args.workers, args.good))
    work = ROOT / 'tmp' / ('curve-%d' % args.seed)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    db, service = build_world(args.seed, work, report_detail='none')
    try:
        for month in range(args.warmup):
            service.advance_turn(command_id='cv-warm-%d' % month, source='audit')
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            for month in range(args.months):
                engine = service.economy_state().engine
                planner = AgentTurn(pool, batch=32)
                engine.step(enterprise_planner=planner)
                if month == args.months-1:
                    report(engine, planner, args.good)
                service.advance_turn(command_id='cv-%d' % month, source='audit')
    finally:
        db.close()
        shutil.rmtree(work, ignore_errors=True)


def report(engine, planner, good):
    print()
    print('=== месяц %d, товар %s' % (engine.meta['month'], good))
    firms = [e for e in engine.enterprises.values() if e['good'] == good]
    print('  %-12s %-8s %-9s %-9s %-9s %-8s'
          % ('фирма', 'цель', 'input', 'нужно', 'доступно', 'куплено'))
    for firm in firms[:8]:
        trace = planner.traces.get(firm['id'])
        if not trace:
            continue
        for row in trace.get('reasons') or []:
            name = engine.goods  # index → name
            good_name = list(engine.goods)[row['good']]
            print('  %-12s %-8.1f %-9s %-9.1f %-9.1f %-8.1f (affordable %.1f, bid %.4f)'
                  % (firm['id'], trace.get('target', 0.0), good_name, row['needed'],
                     row['available'], row['bought'], row['affordable'], row['bid']))
    # Отклонённые заявки по причине.
    rejected = [o for o in engine.report['orders']
                if o['side'] == 'buy' and o.get('reason') == 'landed_price_above_limit']
    print()
    print('  rejected landed_price_above_limit: %d заявок, недополучено %.1f'
          % (len(rejected), sum(o['remaining'] for o in rejected)))
    by_good = Counter()
    for order in rejected:
        by_good[order['good']] += order['remaining']
    for name, quantity in by_good.most_common(5):
        print('    %-8s %.1f' % (name, quantity))
    # Что реально на рынке.
    for input_good in ('flour',):
        curve_rows = []
        for firm in firms[:1]:
            row = list(engine.enterprises).index(firm['id'])
        sells = [o for o in engine.report['orders']
                 if o['side'] == 'sell' and o['good'] == input_good
                 and o['remaining'] > 1e-9]
        print('  на рынке %s: %d офферов, %.1f единиц, цены %.4f..%.4f'
              % (input_good, len(sells), sum(o['remaining'] for o in sells),
                 min((o['price'] for o in sells), default=0),
                 max((o['price'] for o in sells), default=0)))


if __name__ == '__main__':
    main()
