"""Почему заявки на дерево остаются неисполненными при полном складе.

Для каждого неисполненного остатка: причина, цена заявки, лучшая landed-цена
по книге для площадки покупателя и сколько денег зарезервировано.
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
    parser.add_argument('--months', type=int, default=12)
    parser.add_argument('--warmup', type=int, default=2)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--good', default='wood')
    parser.add_argument('--modes', default='v3,pool')
    args = parser.parse_args()

    print('PARAMS: seed=%d months=%d warmup=%d workers=%d good=%s detail=none'
          % (args.seed, args.months, args.warmup, args.workers, args.good))

    for mode in args.modes.split(','):
        work = ROOT / 'tmp' / ('uw-%s-%d' % (mode, args.seed))
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        db, service = build_world(args.seed, work, report_detail='none')
        try:
            for month in range(args.warmup):
                service.advance_turn(command_id='uw-warm-%d' % month, source='audit')
            with ProcessPoolExecutor(max_workers=args.workers) as pool:
                for month in range(args.months):
                    engine = service.economy_state().engine
                    planner = AgentTurn(pool, batch=32) if mode == 'pool' else None
                    engine.step(enterprise_planner=planner)
                    if month == args.months-1:
                        report(engine, mode, args.good)
                    service.advance_turn(command_id='uw-%d' % month, source='audit')
        finally:
            db.close()
            shutil.rmtree(work, ignore_errors=True)


def report(engine, mode, good):
    print()
    print('=== %s, месяц %d' % (mode.upper(), engine.meta['month']))
    unmet = [o for o in engine.report['orders']
             if o['side'] == 'buy' and o['good'] == good
             and o['status'] not in ('cancelled', 'expired')
             and o['remaining'] > 1e-9]
    reasons = Counter(o.get('reason') or 'нет причины' for o in unmet)
    total_unmet = sum(o['remaining'] for o in unmet)
    print('  неисполнено %.1f у %d заявок: %s'
          % (total_unmet, len(unmet), ', '.join('%s=%d' % i for i in reasons.most_common())))

    # Для каждой неисполненной заявки: landed-цена лучшего оффера для неё.
    sells = [o for o in engine.report['orders']
             if o['side'] == 'sell' and o['good'] == good
             and o['status'] not in ('cancelled', 'expired') and o['remaining'] > 1e-9]
    print('  живых офферов %d, сумма %.1f' % (len(sells), sum(o['remaining'] for o in sells)))
    rows = []
    for order in unmet:
        account = engine.accounts[order['participant']]
        tile = engine.market_tile(account)
        best = None
        reachable_offers = 0
        for sell in sells:
            quote = engine.landed(sell['tile_id'], tile, good, sell['price'])
            if quote is None:
                continue
            reachable_offers += 1
            if best is None or quote[0] < best[0]:
                best = (quote[0], sell['participant'], quote[1])
        rows.append((order['remaining'], order['participant'], order['price'],
                     best, reachable_offers, order.get('reserved', 0.0),
                     order.get('reason') or ''))
    rows.sort(reverse=True)
    print('  %-12s %-8s %-10s %-10s %-8s %s'
          % ('покупатель', 'остаток', 'предел', 'лучшая', 'доступно', 'причина'))
    for row in rows[:10]:
        best = row[3]
        print('  %-12s %-8.2f %-10.6f %-10s %-8d %s'
              % (row[1], row[0], row[2],
                 '%.6f' % best[0] if best else 'нет',
                 row[4], row[6][:28]))
    # Сколько дерева лежит на складах у не-продавцов.
    holders = defaultdict(float)
    for account in engine.accounts.values():
        quantity = account['stock'].get(good, 0.0)
        if quantity > 1e-9:
            holders[account['id']] = quantity
    print('  держателей %d, всего %.1f; крупнейшие: %s'
          % (len(holders), sum(holders.values()),
             sorted(holders.items(), key=lambda x: -x[1])[:5]))


if __name__ == '__main__':
    main()
