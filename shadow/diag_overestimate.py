"""Переоценка спроса: сколько каждый агент считает «своим» против реального.

Суммирует `observed` (reachable_demand) по всем производителям товара и
сравнивает с реальным публичным спросом. Если сумма кратно больше спроса,
одновременное планирование завышает выпуск и съедает входы.
"""
import argparse
import shutil
import sys
from collections import defaultdict
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
    parser.add_argument('--months', type=int, default=12)
    parser.add_argument('--warmup', type=int, default=2)
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()

    print('PARAMS: seed=%d months=%d warmup=%d workers=%d detail=none'
          % (args.seed, args.months, args.warmup, args.workers))
    work = ROOT / 'tmp' / ('ov-%d' % args.seed)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    db, service = build_world(args.seed, work, report_detail='none')
    try:
        for month in range(args.warmup):
            service.advance_turn(command_id='ov-warm-%d' % month, source='audit')
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            for month in range(args.months):
                engine = service.economy_state().engine
                planner = AgentTurn(pool, batch=32)
                engine.step(enterprise_planner=planner)
                if month == args.months-1:
                    report(engine, planner)
                service.advance_turn(command_id='ov-%d' % month, source='audit')
    finally:
        db.close()
        shutil.rmtree(work, ignore_errors=True)


def report(engine, planner):
    demand = defaultdict(float)
    for order in engine.report['orders']:
        if order['side'] == 'buy':
            demand[order['good']] += order['quantity']

    print()
    print('=== месяц %d (pool)' % engine.meta['month'])
    print('  %-8s %-5s %-11s %-11s %-9s %-9s'
          % ('товар', 'фирм', 'сумма observed', 'реальный спрос', 'отношение', 'план'))
    rows = defaultdict(lambda: dict(count=0, observed=0.0, plan=0.0))
    for firm in engine.enterprises.values():
        if firm['status'] == 'closed':
            continue
        trace = planner.traces.get(firm['id'])
        if not trace:
            continue
        row = rows[firm['good']]
        row['count'] += 1
        row['observed'] += trace.get('observed', 0.0)
        row['plan'] += trace.get('target', 0.0)
    for good in sorted(rows, key=lambda g: -rows[g]['observed']):
        row = rows[good]
        real = demand.get(good, 0.0)
        ratio = row['observed']/real if real > 1e-9 else float('inf')
        print('  %-8s %-5d %-11.0f %-11.0f %-9.2f %-9.0f'
              % (good, row['count'], row['observed'], real, ratio, row['plan']))
    total_observed = sum(row['observed'] for row in rows.values())
    total_demand = sum(demand.values())
    total_plan = sum(row['plan'] for row in rows.values())
    print()
    print('  ИТОГО: observed %.0f, спрос %.0f, план %.0f -> переоценка %.2fx'
          % (total_observed, total_demand, total_plan,
             total_observed/max(1e-9, total_demand)))


if __name__ == '__main__':
    main()
