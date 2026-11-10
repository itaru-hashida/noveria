"""Почему зерно не доходит до мельниц: цена, деньги или отсутствие заявки.

Для зерна: спрос, предложение, исполнение и по каждой неисполненной заявке —
предел цены против лучшей landed, свободная касса покупателя, зарезервировано.
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
    parser.add_argument('--months', type=int, default=36)
    parser.add_argument('--warmup', type=int, default=2)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--good', default='grain')
    parser.add_argument('--modes', default='v3,pool')
    args = parser.parse_args()

    print('PARAMS: seed=%d months=%d warmup=%d workers=%d good=%s detail=none'
          % (args.seed, args.months, args.warmup, args.workers, args.good))

    for mode in args.modes.split(','):
        work = ROOT / 'tmp' / ('gr2-%s-%d' % (mode, args.seed))
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        db, service = build_world(args.seed, work, report_detail='none')
        try:
            for month in range(args.warmup):
                service.advance_turn(command_id='g2-warm-%d' % month, source='audit')
            with ProcessPoolExecutor(max_workers=args.workers) as pool:
                for month in range(args.months):
                    engine = Economy.load(db)
                    planner = AgentTurn(pool, batch=32) if mode == 'pool' else None
                    engine.step(enterprise_planner=planner)
                    if month == args.months-1:
                        report(engine, mode, args.good)
                    service.advance_turn(command_id='g2-%d' % month, source='audit')
        finally:
            db.close()
            shutil.rmtree(work, ignore_errors=True)


def report(engine, mode, good):
    print()
    print('=== %s, месяц %d, товар %s' % (mode.upper(), engine.meta['month'], good))
    sells = [o for o in engine.report['orders'] if o['side'] == 'sell' and o['good'] == good]
    buys = [o for o in engine.report['orders'] if o['side'] == 'buy' and o['good'] == good]
    trades = [t for t in engine.report['trades'] if t['good'] == good]
    print('  офферов %d (%.1f), заявок %d (%.1f), сделок %d (%.1f)'
          % (len(sells), sum(o['quantity'] for o in sells),
             len(buys), sum(o['quantity'] for o in buys),
             len(trades), sum(t['quantity'] for t in trades)))
    reasons = Counter(o.get('reason') or ('filled' if o['remaining'] <= 1e-9 else '-')
                      for o in buys)
    print('  причины заявок: %s' % ', '.join('%s=%d' % i for i in reasons.most_common(6)))

    # Дешёвые офферы и дорогие пределы: сопоставление.
    if sells:
        prices = sorted(o['price'] for o in sells)
        print('  цены офферов: %.4f .. %.4f (медиана %.4f)'
              % (prices[0], prices[-1], prices[len(prices)//2]))
    if buys:
        prices = sorted(o['price'] for o in buys)
        print('  пределы заявок: %.4f .. %.4f (медиана %.4f)'
              % (prices[0], prices[-1], prices[len(prices)//2]))
        cheapest = min((o['price'] for o in sells), default=0.0)
        too_low = [o for o in buys if o['price'] < cheapest-1e-9]
        print('  заявок с пределом ниже самого дешёвого оффера: %d (спрос %.1f)'
              % (len(too_low), sum(o['quantity'] for o in too_low)))

    # Касса мельниц: сколько свободно и сколько просили.
    mills = [e for e in engine.enterprises.values() if e['good'] == 'flour']
    cash = [engine.free_cash(m['id']) for m in mills]
    orders = [o for o in buys if o['participant'] in {m['id'] for m in mills}]
    print('  мельниц %d: свободная касса мин %.2f, медиана %.2f, макс %.2f'
          % (len(mills), min(cash), sorted(cash)[len(cash)//2], max(cash)))
    print('  их заявок %d на %.1f, зарезервировано %.1f'
          % (len(orders), sum(o['quantity'] for o in orders),
             sum(o.get('reserved', 0.0) for o in orders)))
    unsatisfied = [o for o in orders if o['remaining'] > 1e-9]
    print('  не исполнено у мельниц: %.1f' % sum(o['remaining'] for o in unsatisfied))

    # Кто держит зерно и выставил ли.
    holders = defaultdict(float)
    for account in engine.accounts.values():
        quantity = account['stock'].get(good, 0.0)
        if quantity > 1e-9:
            holders[account['id']] = quantity
    offered = {o['participant'] for o in sells}
    unoffered = {k: v for k, v in holders.items() if k not in offered}
    print('  держателей %d (%.1f), из них без оффера %d (%.1f)'
          % (len(holders), sum(holders.values()), len(unoffered), sum(unoffered.values())))
    for account_id, quantity in sorted(unoffered.items(), key=lambda x: -x[1])[:5]:
        firm = engine.enterprises.get(account_id)
        status = '%s/%s' % (firm['good'], firm['status']) if firm else 'не фирма'
        print('     %-12s %8.1f  %s' % (account_id, quantity, status))

    # Товарный путь: выпуск, склад, сделки.
    makers = [e for e in engine.enterprises.values() if e['good'] == good]
    print('  производители: выпуск %.1f, на складах %.1f'
          % (sum(m.get('output', 0.0) for m in makers),
             sum(engine.accounts[m['id']]['stock'].get(good, 0.0) for m in makers)))


if __name__ == '__main__':
    main()
