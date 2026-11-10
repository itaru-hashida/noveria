"""Предельная цена закупки против landed: арифметика на живой мельнице.

Показывает, почему заявка на зерно получает landed_price_above_limit:
цена продажи муки, издержки, предел по марже и реальная landed-цена зерна.
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
    parser.add_argument('--warmup', type=int, default=8)
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()

    work = ROOT / 'tmp' / ('ceil-%d' % args.seed)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    db, service = sr.build_world(args.seed, work, report_detail='none')
    try:
        for month in range(args.warmup):
            service.advance_turn(command_id='ce-warm-%d' % month, source='audit')

        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            engine = Economy.load(db)
            turn = AgentTurn(pool=pool, batch=32)
            engine.step(enterprise_planner=turn)

        cfg = engine.cfg
        print('PARAMS: seed=%d warmup=%d workers=%d, месяц %d'
              % (args.seed, args.warmup, args.workers, engine.meta['month']))
        print('wage %.3f, markup %.2f, transport %.3f, tax %.2f'
              % (cfg['wage'], cfg['markup'], cfg['transport_rate'],
                 cfg['consumption_tax']))

        # Мельницы, у которых заявка на зерно не прошла по цене.
        targets = []
        for order in engine.report['orders']:
            if (order['side'] == 'buy' and order['good'] == 'grain'
                    and order['reason'] == 'landed_price_above_limit'
                    and order['remaining'] > 1e-6):
                targets.append(order)
        print()
        print('заявок на зерно, не прошедших по цене: %d' % len(targets))
        for order in targets[:4]:
            firm = engine.enterprises[order['participant']]
            account = engine.accounts[firm['id']]
            trace = turn.traces.get(firm['id'], {})
            tile = engine.market_tile(account)
            # Лучшая landed-цена зерна именно для этой площадки.
            best = None
            for other in engine.report['orders']:
                if other['side'] != 'sell' or other['good'] != 'grain':
                    continue
                quote = engine.landed(other['tile_id'], tile, 'grain', other['price'])
                if quote and (best is None or quote[0] < best[0]):
                    best = quote
            print()
            print('мельница %s: цена муки %.4f' % (firm['id'], firm['price']))
            print('   заявка: q=%.2f price=%.4f remaining=%.2f'
                  % (order['quantity'], order['price'], order['remaining']))
            print('   предельная цена по марже (trace) = %.4f, котировка = %.4f'
                  % (order['price'], trace.get('unit_cost', 0.0)))
            print('   лучшая landed-цена зерна для её площадки: %s'
                  % ('%.4f (freight %.4f, dist %s)' % (best[0], best[1], best[2])
                     if best else 'нет маршрута'))
            print('   trace: %s' % {k: trace[k] for k in
                                    ('price', 'unit_cost', 'target', 'purchases')
                                    if k in trace})
            recipe = engine.goods['flour']
            print('   рецепт муки: labor %.2f, inputs %s, цена %.2f'
                  % (recipe['labor'], recipe['inputs'], recipe['price']))
    finally:
        db.close()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    main()
