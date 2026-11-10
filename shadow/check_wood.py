"""Шесть гипотез о распределении дерева, каждая с числом.

1. Дерево уходит в строительство до производственных заявок.
2. Инструменты резервируют дерево через оборудование.
3. Доски получают весь доступный лес.
4. Заявки с одинаковой ценой разрешаются FIFO при разной предельной ценности.
5. Снимок и живой исполнитель видят одинаковую книгу дерева.
6. Остатки дерева в публичных офферах не дублируются.

Запуск:
    python -m shadow.check_wood --seed 73129 --months 12 --warmup 2 --workers 4
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
from shadow import agent_snapshot  # noqa: E402
from shadow.turn import AgentTurn  # noqa: E402


def wood_book(engine):
    """Кто предлагает дерево и кто просит: по назначению и по цене."""
    sells = [o for o in engine.report['orders'] if o['side'] == 'sell' and o['good'] == 'wood']
    buys = [o for o in engine.report['orders'] if o['side'] == 'buy' and o['good'] == 'wood']
    by_purpose = defaultdict(lambda: dict(q=0.0, got=0.0))
    for order in buys:
        by_purpose[order['purpose']]['q'] += order['quantity']
        by_purpose[order['purpose']]['got'] += order['quantity']-order['remaining']
    return dict(sells=sells, buys=buys, by_purpose=dict(by_purpose))


def check(engine, planner, label):
    print()
    print('=== %s, месяц %d' % (label, engine.meta['month']))
    book = wood_book(engine)
    trades = [t for t in engine.report['trades'] if t['good'] == 'wood']
    print('  дерево: офферов %d (%.1f), заявок %d (%.1f), сделок %d (%.1f)'
          % (len(book['sells']), sum(o['quantity'] for o in book['sells']),
             len(book['buys']), sum(o['quantity'] for o in book['buys']),
             len(trades), sum(t['quantity'] for t in trades)))
    print('  спрос по назначению: %s'
          % ', '.join('%s=%.1f (получено %.1f)' % (k, v['q'], v['got'])
                      for k, v in sorted(book['by_purpose'].items(), key=lambda x: -x[1]['q'])))

    # 1. Строительство до производственных заявок: порядок id заявок.
    construction = [o for o in book['buys'] if 'стройк' in o['purpose']
                    or 'Строительные' in o['purpose'] or 'Материалы' in o['purpose']]
    production = [o for o in book['buys'] if o['purpose'] == 'Сырьё']
    if construction or production:
        first_construction = min((int(o['id'].split('-')[-1]) for o in construction), default=None)
        first_production = min((int(o['id'].split('-')[-1]) for o in production), default=None)
        print('  1) первые id: строительство %s, производство %s -> строительство раньше: %s'
              % (first_construction, first_production,
                 first_construction is not None and (first_production is None
                                                     or first_construction < first_production)))

    # 2. Инструменты через оборудование: сколько дерева нужно как оборудование.
    equip_wood = 0.0
    for firm in engine.enterprises.values():
        recipe = engine.goods[firm['good']]
        ratio = recipe.get('equipment', {}).get('wood', 0.0)
        if ratio:
            equip_wood += ratio*max(firm.get('output', 0.0), 0.0)
    print('  2) дерево как оборудование: %.2f (доля от сделок %.1f%%)'
          % (equip_wood, 100*equip_wood/max(1e-9, sum(t['quantity'] for t in trades))))

    # 3. Доски получают весь доступный лес: спрос planks на wood и исполнение.
    planks_demand = book['by_purpose'].get('Сырьё', {}).get('q', 0.0)
    planks_firms = [e for e in engine.enterprises.values() if e['good'] == 'planks']
    planks_wood_need = sum(max(0.0, (e.get('production_plan') or {}).get('output', 0.0))
                           for e in planks_firms)*1.3
    print('  3) доски: план %s, нужно дерева %.1f'
          % (round(sum((e.get('production_plan') or {}).get('output', 0.0)
                       for e in planks_firms), 1), planks_wood_need))

    # 4. Одинаковая цена у разных покупателей: кто получил и кому отказали.
    by_price = defaultdict(lambda: dict(asks=[], got=0.0))
    for order in book['buys']:
        by_price[round(order['price'], 6)]['asks'].append(
            (order['participant'], order['quantity'], order['quantity']-order['remaining']))
    ties = {price: row for price, row in by_price.items() if len(row['asks']) > 1}
    if ties:
        price, row = max(ties.items(), key=lambda item: len(item[1]['asks']))
        got = sum(a[2] for a in row['asks'])
        total = sum(a[1] for a in row['asks'])
        print('  4) цена %.6f: %d покупателей, спрос %.1f, получено %.1f (%.0f%%)'
              % (price, len(row['asks']), total, got, 100*got/total if total else 0))
        for participant, quantity, filled in row['asks'][:5]:
            good = engine.enterprises.get(participant, {}).get('good', '-')
            print('       %s (%s) %.1f -> %.1f' % (participant, good, quantity, filled))

    # 5. Снимок против живой книги: одинаковое ли предложение дерева.
    sells = defaultdict(list)
    for order in engine.report['orders']:
        if order['side'] == 'sell':
            sells[order['good']].append(order)
    budget = {f['id']: engine.free_cash(f['id']) for f in engine.enterprises.values()}
    arrays, meta = agent_snapshot.build(engine, budget, sells=sells)
    if 'wood' in meta['good_index']:
        column = meta['good_index']['wood']
        in_snapshot = int((arrays['offer_good'] == column).sum())
        in_live = len(book['sells'])
        snapshot_qty = float(arrays['offer_remaining'][arrays['offer_good'] == column].sum())
        live_qty = sum(o['remaining'] for o in book['sells'])
        print('  5) снимок: %d офферов %.2f | живая книга: %d офферов %.2f'
              % (in_snapshot, snapshot_qty, in_live, live_qty))

    # 6. Дублирование: сумма stock по счетам против суммы офферов по дереву.
    stock_total = engine.total_stock().get('wood', 0.0)
    offered = sum(o['quantity'] for o in book['sells'])
    print('  6) склад мира %.1f, выставлено %.1f (доля %.0f%%), зарезервировано %.1f'
          % (stock_total, offered, 100*offered/max(1e-9, stock_total),
             sum(o['remaining'] for o in book['buys'])))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int, default=73129)
    parser.add_argument('--months', type=int, default=12)
    parser.add_argument('--warmup', type=int, default=2)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--modes', default='v3,pool')
    parser.add_argument('--at', default='-1', help='индексы месяцев через запятую, -1 последний')
    args = parser.parse_args()

    print('PARAMS: seed=%d months=%d warmup=%d workers=%d modes=%s detail=none'
          % (args.seed, args.months, args.warmup, args.workers, args.modes))
    want = {int(v) if int(v) >= 0 else args.months-1 for v in args.at.split(',')}

    for mode in args.modes.split(','):
        work = ROOT / 'tmp' / ('cw-%s-%d' % (mode, args.seed))
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        db, service = build_world(args.seed, work, report_detail='none')
        try:
            for month in range(args.warmup):
                service.advance_turn(command_id='cw-warm-%d' % month, source='audit')
            with ProcessPoolExecutor(max_workers=args.workers) as pool:
                for month in range(args.months):
                    engine = Economy.load(db)
                    planner = AgentTurn(pool, batch=32) if mode == 'pool' else None
                    engine.step(enterprise_planner=planner)
                    if month in want:
                        check(engine, planner, mode.upper())
                    service.advance_turn(command_id='cw-%d' % month, source='audit')
        finally:
            db.close()
            shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    main()
