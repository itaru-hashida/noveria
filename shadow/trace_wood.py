"""Трассировка дерева по месяцам: кто получает дефицитный ресурс и почему.

По каждому товару, использующему wood (planks, iron, tools, bread, строительство),
показывается:

    начальный запас · выпуск · публичное предложение · спрос по назначению ·
    зарезервированное · исполненные сделки · остаток

Плюс разбор конкуренции за каждую единицу дерева: кто покупатели, с какой
ценой, в каком порядке они получают остаток.

Запуск:
    python -m shadow.trace_wood --seed 73129 --months 12 --warmup 2 --workers 4
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

WOOD_USERS = ('planks', 'iron', 'tools', 'bread', 'flour')


def snapshot_book(engine, good):
    """Состояние книги по товару: предложение, спрос, сделки, остатки."""
    sells = [o for o in engine.report['orders'] if o['side'] == 'sell' and o['good'] == good]
    buys = [o for o in engine.report['orders'] if o['side'] == 'buy' and o['good'] == good]
    trades = [t for t in engine.report['trades'] if t['good'] == good]
    offered = sum(o['quantity'] for o in sells)
    requested = sum(o['quantity'] for o in buys)
    reserved = sum(o['remaining'] for o in buys)
    executed = sum(t['quantity'] for t in trades)
    return dict(offered=offered, requested=requested, executed=executed,
                unmet=reserved, trades=len(trades),
                sells=sells, buys=buys)


def buyers_by_purpose(engine, good):
    """Спрос по назначению и товару покупателя: кто тянет ресурс."""
    rows = defaultdict(lambda: dict(quantity=0.0, executed=0.0, unmet=0.0, count=0))
    purposes = {o['id']: o['purpose'] for o in engine.report['orders'] if o['side'] == 'buy'}
    for order in engine.report['orders']:
        if order['side'] != 'buy' or order['good'] != good:
            continue
        key = (order['purpose'], order.get('participant'))
        rows[key]['quantity'] += order['quantity']
        rows[key]['unmet'] += order['remaining']
        rows[key]['count'] += 1
    for trade in engine.report['trades']:
        if trade['good'] != good:
            continue
        key = (trade['purpose'], trade['buyer'])
        if key in rows:
            rows[key]['executed'] += trade['quantity']
    return rows


def price_levels(engine, good):
    """Исполненные сделки по уровням цены: кто по какой цене получил ресурс."""
    levels = defaultdict(lambda: dict(quantity=0.0, buyers=Counter()))
    for trade in engine.report['trades']:
        if trade['good'] != good:
            continue
        row = levels[round(trade['price'], 6)]
        row['quantity'] += trade['quantity']
        row['buyers'][trade['purpose']] += trade['quantity']
    return dict(sorted(levels.items()))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int, default=73129)
    parser.add_argument('--months', type=int, default=12)
    parser.add_argument('--warmup', type=int, default=2)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--batch', type=int, default=32)
    parser.add_argument('--modes', default='v3,pool')
    parser.add_argument('--tail', type=int, default=3, help='сколько последних месяцев печатать')
    args = parser.parse_args()

    print('PARAMS: seed=%d months=%d warmup=%d workers=%d batch=%d modes=%s detail=none'
          % (args.seed, args.months, args.warmup, args.workers, args.batch, args.modes))

    for mode in args.modes.split(','):
        work = ROOT / 'tmp' / ('trace-wood-%s-%d' % (mode, args.seed))
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        db, service = build_world(args.seed, work, report_detail='none')
        history = []
        try:
            for month in range(args.warmup):
                service.advance_turn(command_id='tw-warm-%d' % month, source='audit')
            with ProcessPoolExecutor(max_workers=args.workers) as pool:
                for month in range(args.months):
                    engine = Economy.load(db)
                    planner = AgentTurn(pool, batch=args.batch) if mode == 'pool' else None
                    opening_wood = engine.total_stock().get('wood', 0.0)
                    engine.step(enterprise_planner=planner)
                    history.append(dict(month=engine.meta['month'], opening=opening_wood,
                                        produced=engine.report['production'].get('wood', 0.0),
                                        closing=engine.total_stock().get('wood', 0.0),
                                        book=snapshot_book(engine, 'wood'),
                                        purposes=buyers_by_purpose(engine, 'wood'),
                                        levels=price_levels(engine, 'wood'),
                                        users={g: snapshot_book(engine, g) for g in WOOD_USERS},
                                        idle=Counter(f.get('reason') or '' for f
                                                     in engine.enterprises.values()
                                                     if f['status'] == 'idle')))
                    service.advance_turn(command_id='tw-%d' % month, source='audit')
        finally:
            db.close()
            shutil.rmtree(work, ignore_errors=True)

        print()
        print('################ %s ################' % mode.upper())
        print('%-5s %-9s %-9s %-9s %-9s %-9s %-9s'
              % ('mon', 'open', 'produced', 'offered', 'requested', 'executed', 'unmet'))
        for row in history:
            book = row['book']
            print('%-5s %-9.1f %-9.1f %-9.1f %-9.1f %-9.1f %-9.1f'
                  % (row['month'], row['opening'], row['produced'], book['offered'],
                     book['requested'], book['executed'], book['unmet']))

        for row in history[-args.tail:]:
            print()
            print('--- месяц %d (%s): открытие %.1f, выпуск %.1f, закрытие %.1f'
                  % (row['month'], mode, row['opening'], row['produced'], row['closing']))
            print('  сделки по wood по уровням цены:')
            for price, level in row['levels'].items():
                print('    %.6f: %.1f  (%s)'
                      % (price, level['quantity'],
                         ', '.join('%s=%.1f' % i for i in level['buyers'].most_common())))
            print('  конкуренты за дерево (назначение, покупатель): спрос / получено / недополучено')
            rows = sorted(row['purposes'].items(), key=lambda item: -item[1]['quantity'])
            for (purpose, participant), data in rows[:10]:
                good = ''
                print('    %-34s %-10s %8.1f / %8.1f / %8.1f'
                      % (purpose[:34], participant or '', data['quantity'],
                         data['executed'], data['unmet']))
            print('  пользователи дерева: выпуск / спрос / сделки')
            for good, book in row['users'].items():
                print('    %-7s выпуск %8.1f  продано %8.1f  спрос %8.1f  недополучено %8.1f'
                      % (good, row['users'][good] and 0.0 or 0.0, book['executed'],
                         book['requested'], book['unmet']))
            print('  простои: %s'
                  % ', '.join('%s=%d' % i for i in row['idle'].most_common(4)))


if __name__ == '__main__':
    main()
