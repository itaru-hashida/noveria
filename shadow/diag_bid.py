"""Почему B2B-заявка не проходит по цене: предел пекарни против landed оффера.

Для одной пекарни показывается: её предельная цена закупки (bid), котировка из
снимка, реальные landed-цены офферов муки и итог match.
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

    work = ROOT / 'tmp' / ('bid-%d' % args.seed)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    db, service = sr.build_world(args.seed, work, report_detail='none')
    try:
        for month in range(args.warmup):
            service.advance_turn(command_id='bid-warm-%d' % month, source='audit')

        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            engine = service.economy_state().engine
            turn = AgentTurn(pool=pool, batch=32)
            engine.step(enterprise_planner=turn)

        print('PARAMS: seed=%d warmup=%d workers=%d, месяц %d'
              % (args.seed, args.warmup, args.workers, engine.meta['month']))

        # Пекарня с наибольшим планом и нулевой мукой.
        bakers = sorted((e for e in engine.enterprises.values() if e['good'] == 'bread'),
                        key=lambda e: -e.get('production_plan', {}).get('output', 0.0))
        firm = bakers[0]
        account = engine.accounts[firm['id']]
        print()
        print('пекарня %s: план %.2f, цена %.3f, мука на складе %.2f, предел: %s'
              % (firm['id'], firm['production_plan'].get('output', 0.0), firm['price'],
                 account['stock'].get('flour', 0.0), firm.get('production_limit')))
        print('трасса планировщика:', turn.traces.get(firm['id']))

        # Её заявка на муку и что стало.
        for order in engine.report['orders']:
            if order['participant'] == firm['id'] and order['good'] == 'flour':
                print('заявка: q=%.2f price=%.4f remaining=%.2f status=%s reason=%s'
                      % (order['quantity'], order['price'], order['remaining'],
                         order['status'], order['reason']))

        # Офферы муки с landed-ценой для её площадки.
        tile = engine.market_tile(account)
        print()
        print('офферы муки и landed-цена для площадки %s:' % tile)
        rows = []
        for order in engine.report['orders']:
            if order['side'] != 'sell' or order['good'] != 'flour':
                continue
            quote = engine.landed(order['tile_id'], tile, 'flour', order['price'])
            landed = quote[0] if quote else None
            rows.append((landed if landed is not None else 1e9, order['participant'],
                         order['price'], order['quantity'], order['remaining'], landed))
        rows.sort()
        for row in rows[:8]:
            print('  %s price=%.4f q=%.2f remaining=%.2f landed=%s'
                  % (row[1], row[2], row[3], row[4],
                     '%.4f' % row[5] if row[5] is not None else 'нет маршрута'))

        # Сравнение с котировкой, которую видел планировщик.
        print()
        print('цена продажи пекарни %.4f, налог %.2f'
              % (firm['price'], engine.cfg['consumption_tax']))
        print('мука: базовая цена %.2f, лучший landed выше' % engine.goods['flour']['price'])
    finally:
        db.close()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    main()
