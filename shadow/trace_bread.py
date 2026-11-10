"""Трассировка цепочки зерно -> мука -> хлеб: где обрывается.

Отделяет четыре разных причины недопотребления хлеба:

1. ошибки исполнения (заявка есть, но не сведена);
2. ошибки ожиданий (прогноз/плана нет при живом спросе);
3. дефицит денег (план есть, касса не пускает);
4. физические ограничения (мощность, труд, сырьё, ресурс).

Запуск:
    python -m shadow.trace_bread --seed 73129 --months 12 --warmup 2 --workers 4
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
from shadow import agent_snapshot  # noqa: E402
from shadow.turn import AgentTurn  # noqa: E402


def chain_report(engine, trace, meta, goods_of_interest=('grain', 'flour', 'bread')):
    """Сводка по каждому товару цепочки: план, выпуск, спрос, сделки."""
    rows = {}
    for good in goods_of_interest:
        rows[good] = dict(planned=0.0, output=0.0, traded=0.0, demand=0.0,
                          firms=0, idle=Counter(), reasons=Counter(), capacity=0.0)
    index = {name: i for i, name in enumerate(meta['goods'])}
    for row, firm in enumerate(meta['firms']):
        good = meta['goods'][int(meta['agent_good_index'][row])] if 'agent_good_index' in meta \
            else meta['goods'][meta['firms_good_index'][row]]
        if good not in rows:
            continue
        rows[good]['firms'] += 1
        rows[good]['capacity'] += float(meta['agent_capacity'][row])
        info = trace.get(firm)
        if info:
            rows[good]['planned'] += info.get('target', 0.0)
            rows[good]['reasons'][info.get('reason', '')] += 1
    for order in engine.report['orders']:
        good = order['good']
        if good in rows:
            rows[good]['demand'] += order['quantity'] if order['side'] == 'buy' else 0.0
    for trade in engine.report['trades']:
        if trade['good'] in rows:
            rows[good] if False else None
            rows[trade['good']]['traded'] += trade['quantity']
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int, default=73129)
    parser.add_argument('--months', type=int, default=12)
    parser.add_argument('--warmup', type=int, default=2)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--batch', type=int, default=32)
    args = parser.parse_args()

    work = ROOT / 'tmp' / ('trace-bread-%d' % args.seed)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    db, service = sr.build_world(args.seed, work, report_detail='none')
    try:
        for month in range(args.warmup):
            service.advance_turn(command_id='tb-warm-%d' % month, source='audit')

        print('PARAMS: seed=%d months=%d warmup=%d detail=none workers=%d batch=%d'
              % (args.seed, args.months, args.warmup, args.workers, args.batch))

        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            for month in range(args.months):
                engine = Economy.load(db)
                turn = AgentTurn(pool=pool, batch=args.batch)
                engine.step(enterprise_planner=turn)
                engine.meta['month']
                report = engine.report

                if month >= args.months-2:
                    # Разбор последних месяцев: спрос, план, выпуск, сделки.
                    traces = turn.traces
                    firms = sorted(traces)
                    print()
                    print('=== месяц %d: предприятий в снимке %d, плановых %d'
                          % (engine.meta['month'], len(firms), len(turn.log)))
                    for good in ('grain', 'flour', 'bread'):
                        makers = [f for f in firms if engine.enterprises[f]['good'] == good]
                        plan_sum = sum(traces[f].get('target', 0.0) for f in makers)
                        cap_sum = sum(engine.enterprises[f]['capacity'] for f in makers)
                        out = sum(engine.enterprises[f].get('output', 0.0) for f in makers)
                        print('  %-6s фирм %3d  мощность %8.1f  план %8.1f  выпуск %8.1f'
                              % (good, len(makers), cap_sum, plan_sum, out))
                        reasons = Counter(traces[f].get('reason', '') for f in makers)
                        print('         причины плана: %s'
                              % ', '.join('%s=%d' % item for item in reasons.most_common(4)))
                        demand = sum(o['quantity'] for o in report['orders']
                                     if o['side'] == 'buy' and o['good'] == good)
                        sold = sum(t['quantity'] for t in report['trades'] if t['good'] == good)
                        print('         спрос %8.1f  сделки %8.1f' % (demand, sold))

                    consumed = report['consumed'].get('bread', 0.0)
                    needs = sum(a.get('needs', {}).get('bread', {}).get('needed', 0.0)
                                for a in engine.accounts.values() if a['kind'] == 'household')
                    deficits = sum(a.get('needs', {}).get('bread', {}).get('deficit', 0.0)
                                   for a in engine.accounts.values() if a['kind'] == 'household')
                    print('  ХЛЕБ: нужно населению %.1f, потреблено %.1f, дефицит %.1f (%.1f%%)'
                          % (needs, consumed, deficits,
                             100*consumed/needs if needs else 0.0))

                    # Трассировка конкретной пекарни и мельницы.
                    for firm_id in makers[:2]:
                        print('  --- %s (%s)' % (firm_id, good))
                        print('      %s' % traces[firm_id])
                    for mill in [f for f in firms if engine.enterprises[f]['good'] == 'flour'][:1]:
                        print('  --- %s (flour)' % mill)
                        print('      %s' % traces[mill])

                service.advance_turn(command_id='tb-%d' % month, source='audit')
    finally:
        db.close()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    main()
