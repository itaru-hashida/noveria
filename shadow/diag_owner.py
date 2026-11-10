"""Проверка хлебной цепочки: кто не получает хлеб и из-за чего.

Разделяет: исполнено, отклонено по цене доставки, осталось на рынке.
Показывает предел собственных заявок против landed-цены офферов.
"""
import statistics
import shutil
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'audit'))

from shadow import sandbox_mp  # noqa: E402

sandbox_mp.enable_multiprocessing()

import argparse  # noqa: E402
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
    parser.add_argument('--good', default='bread')
    args = parser.parse_args()

    print('PARAMS: seed=%d months=%d warmup=%d workers=%d good=%s detail=none'
          % (args.seed, args.months, args.warmup, args.workers, args.good))
    work = ROOT / 'tmp' / ('own-%d' % args.seed)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    db, service = build_world(args.seed, work, report_detail='none')
    try:
        for month in range(args.warmup):
            service.advance_turn(command_id='ow-warm-%d' % month, source='audit')
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            for month in range(args.months):
                engine = service.economy_state().engine
                planner = AgentTurn(pool, batch=32)
                engine.step(enterprise_planner=planner)
                if month == args.months-1:
                    report(engine, args.good)
                service.advance_turn(command_id='ow-%d' % month, source='audit')
    finally:
        db.close()
        shutil.rmtree(work, ignore_errors=True)


def report(engine, good):
    print()
    print('=== месяц %d, товар %s' % (engine.meta['month'], good))
    offers = [o for o in engine.report['orders'] if o['side'] == 'sell' and o['good'] == good]
    bids = [o for o in engine.report['orders'] if o['side'] == 'buy' and o['good'] == good]
    trades = [t for t in engine.report['trades'] if t['good'] == good]
    print('  выпуск %.1f, выставлено %.1f, продано %.1f, осталось %.1f'
          % (engine.report['production'].get(good, 0.0),
             sum(o['quantity'] for o in offers), sum(t['quantity'] for t in trades),
             sum(o['remaining'] for o in offers)))
    by_kind = Counter()
    for trade in trades:
        firm = engine.enterprises.get(trade['buyer'])
        by_kind[firm['good'] if firm else engine.accounts[trade['buyer']]['kind']] += trade['quantity']
    print('  куплено: %s' % dict(by_kind))
    kinds = Counter()
    for order in bids:
        firm = engine.enterprises.get(order['participant'])
        kinds[firm['good'] if firm else engine.accounts[order['participant']]['kind']] += 1
    print('  заявок: %s' % dict(kinds))
    print('  причины: %s'
          % ', '.join('%s=%d' % item for item in Counter(
              o.get('reason') or o['status'] for o in bids).most_common(5)))

    # Собственные заявки: предел против landed.
    owners = [o for o in bids if engine.accounts[o['participant']]['kind'] in ('private', 'merchant')]
    if owners and offers:
        prices = sorted(o['price'] for o in offers)
        print('  офферы: min %.4f медиана %.4f max %.4f'
              % (prices[0], statistics.median(prices), prices[-1]))
        limits = sorted(o['price'] for o in owners)
        print('  пределы собственников: min %.4f медиана %.4f max %.4f'
              % (limits[0], statistics.median(limits), limits[-1]))
        gaps = []
        for order in owners[:6]:
            account = engine.accounts[order['participant']]
            tile = engine.market_tile(account)
            best = None
            for offer in offers:
                quote = engine.landed(offer['tile_id'], tile, good, offer['price'])
                if quote and (best is None or quote[0] < best):
                    best = quote[0]
            if best:
                gaps.append(order['price']-best)
                print('    %-18s bid %.4f landed %.4f gap %+.4f (%s)'
                      % (order['participant'], order['price'], best,
                         order['price']-best, order.get('reason') or order['status']))
        if gaps:
            print('    медианный разрыв предела и доставки: %+.4f' % statistics.median(gaps))


if __name__ == '__main__':
    main()
