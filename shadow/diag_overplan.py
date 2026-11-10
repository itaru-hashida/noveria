"""Суммарный план против реального спроса: переоценка спроса при одновременном плане.

Проверяет гипотезу: при одновременном планировании каждый агент видит весь
публичный спрос и планирует под него, поэтому суммарный план кратно превышает
реальный спрос. V3 планирует последовательно: каждая следующая фирма видит
офферы уже спланировавших и планирует меньше.

Запуск:
    python -m shadow.diag_overplan --seed 73129 --months 12 --warmup 2 --workers 4
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
    parser.add_argument('--modes', default='v3,pool')
    args = parser.parse_args()

    print('PARAMS: seed=%d months=%d warmup=%d workers=%d detail=none'
          % (args.seed, args.months, args.warmup, args.workers))
    for mode in args.modes.split(','):
        work = ROOT / 'tmp' / ('op-%s-%d' % (mode, args.seed))
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        db, service = build_world(args.seed, work, report_detail='none')
        try:
            for month in range(args.warmup):
                service.advance_turn(command_id='op-warm-%d' % month, source='audit')
            with ProcessPoolExecutor(max_workers=args.workers) as pool:
                for month in range(args.months):
                    engine = Economy.load(db)
                    planner = AgentTurn(pool, batch=32) if mode == 'pool' else None
                    engine.step(enterprise_planner=planner)
                    if month == args.months-1:
                        report(engine, planner, mode)
                    service.advance_turn(command_id='op-%d' % month, source='audit')
        finally:
            db.close()
            shutil.rmtree(work, ignore_errors=True)


def report(engine, planner, mode):
    print()
    print('=== %s, месяц %d' % (mode.upper(), engine.meta['month']))
    # Суммарный план и реальный спрос по товарам с несколькими производителями.
    plan = defaultdict(float)
    capacity = defaultdict(float)
    counts = defaultdict(int)
    observed = defaultdict(float)
    for firm in engine.enterprises.values():
        if firm['status'] == 'closed':
            continue
        good = firm['good']
        planned = (firm.get('production_plan') or {}).get('output', 0.0)
        plan[good] += planned
        capacity[good] += firm['capacity']
        counts[good] += 1
        observed[good] += firm.get('reachable_demand', 0.0) or 0.0
    demand = defaultdict(float)
    for order in engine.report['orders']:
        if order['side'] == 'buy':
            demand[order['good']] += order['quantity']
    executed = defaultdict(float)
    for trade in engine.report['trades']:
        executed[trade['good']] += trade['quantity']

    print('  %-8s %-5s %-9s %-9s %-9s %-9s %-9s'
          % ('товар', 'фирм', 'мощность', 'план', 'спрос', 'сделки', 'план/спрос'))
    for good in sorted(plan, key=lambda g: -plan[g]):
        ratio = plan[good]/demand[good] if demand[good] > 1e-9 else float('inf')
        print('  %-8s %-5d %-9.0f %-9.0f %-9.0f %-9.0f %-9.2f'
              % (good, counts[good], capacity[good], plan[good], demand[good],
                 executed[good], ratio))
    print()
    print('  суммарный ПЛАН / сумма мощностей: %.0f / %.0f = %.2f'
          % (sum(plan.values()), sum(capacity.values()),
             sum(plan.values())/max(1e-9, sum(capacity.values()))))
    # Для бread: сколько каждая пекарня запланировала.
    bakers = [e for e in engine.enterprises.values() if e['good'] == 'bread']
    targets = sorted(((e.get('production_plan') or {}).get('output', 0.0) for e in bakers),
                     reverse=True)
    print('  пекарни: план по убыванию %s' % [round(v, 1) for v in targets[:10]])


if __name__ == '__main__':
    main()
