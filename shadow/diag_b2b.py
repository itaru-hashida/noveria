"""Почему B2B-заявка на муку не исполняется: цена, книга или маршрут.

Смотрит одну пекарню и одну мельницу в одном месяце: что просит пекарня, по
какой цене, что лежит в книге предложений муки, что произошло после match.
"""
import argparse
import shutil
import sys
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
    parser.add_argument('--warmup', type=int, default=6)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--batch', type=int, default=32)
    args = parser.parse_args()

    work = ROOT / 'tmp' / ('b2b-%d' % args.seed)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    db, service = sr.build_world(args.seed, work, report_detail='none')
    try:
        for month in range(args.warmup):
            service.advance_turn(command_id='b2b-warm-%d' % month, source='audit')

        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            engine = service.economy_state().engine
            turn = AgentTurn(pool=pool, batch=args.batch)
            engine.step(enterprise_planner=turn)

        print('PARAMS: seed=%d warmup=%d workers=%d batch=%d, месяц %d'
              % (args.seed, args.warmup, args.workers, args.batch, engine.meta['month']))

        # Заявки на муку: что просили, по какой цене, что стало.
        flour_buys = [o for o in engine.report['orders']
                      if o['side'] == 'buy' and o['good'] == 'flour']
        print()
        print('заявок на покупку муки: %d' % len(flour_buys))
        for order in flour_buys[:6]:
            print('  %s %-10s q=%8.2f price=%6.3f remaining=%8.2f status=%-10s reason=%s'
                  % (order['participant'], order['purpose'], order['quantity'],
                     order['price'], order['remaining'], order['status'], order['reason']))

        # Книга предложений муки: кто продавал и по какой цене.
        flour_sells = [o for o in engine.report['orders']
                       if o['side'] == 'sell' and o['good'] == 'flour']
        print()
        print('офферов муки: %d' % len(flour_sells))
        for order in flour_sells[:8]:
            print('  %s q=%8.2f price=%6.3f remaining=%8.2f status=%s'
                  % (order['participant'], order['quantity'], order['price'],
                     order['remaining'], order['status']))

        # Сделки по муке.
        flour_trades = [t for t in engine.report['trades'] if t['good'] == 'flour']
        print()
        print('сделок по муке: %d, объём %.2f'
              % (len(flour_trades), sum(t['quantity'] for t in flour_trades)))
        for trade in flour_trades[:5]:
            print('  %s -> %s  %.2f по %.3f' % (trade['seller'], trade['buyer'],
                                                trade['quantity'], trade['price']))

        # Почему не купили: остатки на складах пекарен.
        bakers = [e for e in engine.enterprises.values() if e['good'] == 'bread']
        print()
        print('пекарни: %d' % len(bakers))
        for firm in bakers[:4]:
            account = engine.accounts[firm['id']]
            plan = firm.get('production_plan', {})
            print('  %s stock flour=%7.2f bread=%7.2f plan=%7.2f output=%7.2f limit=%s'
                  % (firm['id'], account['stock'].get('flour', 0.0),
                     account['stock'].get('bread', 0.0), plan.get('output', 0.0),
                     firm.get('output', 0.0), firm.get('production_limit')))

        # Сколько муки просили все пекарни и сколько получили.
        print()
        needed = sum(o['quantity'] for o in flour_buys if o['purpose'] == 'Сырьё')
        bought = sum(t['quantity'] for t in flour_trades
                     if engine.enterprises[t['buyer']]['good'] == 'bread')
        print('муки просили пекарни %.1f, купили %.1f (%.1f%%)'
              % (needed, bought, 100*bought/needed if needed else 0.0))
    finally:
        db.close()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    main()
