"""Где теряется зерно: предложение, спрос, цены и исполнение по звену.

Для зерна и муки: сколько выставлено, сколько просили, сколько сошлось,
какие заявки остались и почему (цена или исчерпание книги).
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

from modules.economy import Economy  # noqa: E402
import scenario_runner as sr  # noqa: E402
from shadow.turn import AgentTurn  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int, default=73129)
    parser.add_argument('--warmup', type=int, default=8)
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()

    work = ROOT / 'tmp' / ('grain-%d' % args.seed)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    db, service = sr.build_world(args.seed, work, report_detail='none')
    try:
        for month in range(args.warmup):
            service.advance_turn(command_id='gr-warm-%d' % month, source='audit')

        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            engine = Economy.load(db)
            turn = AgentTurn(pool=pool, batch=32)
            engine.step(enterprise_planner=turn)

        print('PARAMS: seed=%d warmup=%d workers=%d, месяц %d'
              % (args.seed, args.warmup, args.workers, engine.meta['month']))

        for good in ('grain', 'flour', 'bread'):
            sells = [o for o in engine.report['orders'] if o['side'] == 'sell' and o['good'] == good]
            buys = [o for o in engine.report['orders'] if o['side'] == 'buy' and o['good'] == good]
            trades = [t for t in engine.report['trades'] if t['good'] == good]
            offered = sum(o['quantity'] for o in sells)
            requested = sum(o['quantity'] for o in buys)
            sold = sum(t['quantity'] for t in trades)
            unpaid = sum(o['remaining'] for o in buys)
            reasons = Counter(o['reason'] or o['status'] for o in buys if o['remaining'] > 1e-9)
            print()
            print('=== %s: выставлено %.1f, просили %.1f, сделки %.1f, недополучено %.1f'
                  % (good, offered, requested, sold, unpaid))
            print('    причины неисполненного спроса: %s'
                  % ', '.join('%s=%d' % item for item in reasons.most_common(5)))
            if sells:
                prices = sorted(o['price'] for o in sells)
                print('    цены офферов: min %.4f, медиана %.4f, max %.4f'
                      % (prices[0], prices[len(prices)//2], prices[-1]))
            if buys:
                prices = sorted(o['price'] for o in buys)
                print('    пределы покупателей: min %.4f, медиана %.4f, max %.4f'
                      % (prices[0], prices[len(prices)//2], prices[-1]))
            # Кто производит и сколько осталось на складах.
            makers = [e for e in engine.enterprises.values() if e['good'] == good]
            stock = sum(engine.accounts[e['id']]['stock'].get(good, 0.0) for e in makers)
            reserved = sum(engine.accounts[e['id']]['stock_reserved'].get(good, 0.0)
                           for e in makers)
            print('    у производителей на складе %.1f (зарезервировано %.1f), выпуск %.1f'
                  % (stock, reserved, sum(e.get('output', 0.0) for e in makers)))

        # Зерновики: почему план не выполнен полностью.
        print()
        print('зерновики (план -> выпуск, ограничение):')
        for firm in sorted((e for e in engine.enterprises.values() if e['good'] == 'grain'),
                           key=lambda e: -e['capacity'])[:6]:
            plan = firm.get('production_plan', {})
            print('  %s план %7.1f выпуск %7.1f цена %.3f предел %s'
                  % (firm['id'], plan.get('output', 0.0), firm.get('output', 0.0),
                     firm['price'], firm.get('production_limit')))
        print()
        print('мельницы (план -> выпуск, ограничение):')
        for firm in sorted((e for e in engine.enterprises.values() if e['good'] == 'flour'),
                           key=lambda e: -e['capacity'])[:6]:
            plan = firm.get('production_plan', {})
            print('  %s план %7.1f выпуск %7.1f цена %.3f предел %s'
                  % (firm['id'], plan.get('output', 0.0), firm.get('output', 0.0),
                     firm['price'], firm.get('production_limit')))
    finally:
        db.close()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    main()
