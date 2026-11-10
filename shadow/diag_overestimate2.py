"""Переоценка спроса: как делить публичный спрос между конкурентами.

Гипотеза: каждый агент видит весь публичный спрос своей отрасли и планирует
под него, поэтому суммарный план превышает спрос в 1,67-6 раз. V3 планирует
последовательно, и каждая следующая фирма видит офферы предыдущих.

Проверяет три варианта деления:
  A. как сейчас — весь спрос каждомe;
  B. на число действующих продавцов товара (равная доля);
  C. на число продавцов, которые ещё имеют шанс (не выкупили свою долю).

Замеряет суммарный план и отношение к спросу для каждого.
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
from shadow import agent_planner, agent_snapshot  # noqa: E402
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
    work = ROOT / 'tmp' / ('ov2-%d' % args.seed)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    db, service = build_world(args.seed, work, report_detail='none')
    try:
        for month in range(args.warmup):
            service.advance_turn(command_id='ov2-warm-%d' % month, source='audit')
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            for month in range(args.months):
                engine = Economy.load(db)
                planner = AgentTurn(pool, batch=32)
                engine.step(enterprise_planner=planner)
                if month == args.months-1:
                    analyse(engine, planner)
                service.advance_turn(command_id='ov2-%d' % month, source='audit')
    finally:
        db.close()
        shutil.rmtree(work, ignore_errors=True)


def analyse(engine, planner):
    demand = defaultdict(float)
    for order in engine.report['orders']:
        if order['side'] == 'buy':
            demand[order['good']] += order['quantity']
    # Сколько действующих продавцов каждого товара.
    sellers = defaultdict(int)
    for firm in engine.enterprises.values():
        if firm['status'] != 'closed':
            sellers[firm['good']] += 1

    print()
    print('=== месяц %d' % engine.meta['month'])
    print('  %-8s %-6s %-10s %-10s %-10s %-10s'
          % ('товар', 'фирм', 'спрос', 'A: всем', 'B: доля', 'C: факт'))
    rows = defaultdict(lambda: dict(count=0, observed=0.0, plan=0.0))
    for firm in engine.enterprises.values():
        if firm['status'] == 'closed':
            continue
        trace = planner.traces.get(firm['id'])
        if trace:
            rows[firm['good']]['count'] += 1
            rows[firm['good']]['observed'] += trace.get('observed', 0.0)
            rows[firm['good']]['plan'] += trace.get('target', 0.0)
    for good in sorted(rows, key=lambda g: -rows[g]['observed'])[:6]:
        row = rows[good]
        real = demand.get(good, 0.0)
        count = max(1, sellers[good])
        print('  %-8s %-6d %-10.0f %-10.0f %-10.0f %-10.0f'
              % (good, count, real, row['observed'], real, row['observed']/count))
    print()
    print('  отношение суммы observed к спросу по всем товарам:')
    for good in sorted(rows):
        real = demand.get(good, 0.0)
        if real > 1e-9 and rows[good]['observed'] > 1e-9:
            print('    %-8s %.2fx' % (good, rows[good]['observed']/real))


if __name__ == '__main__':
    main()
