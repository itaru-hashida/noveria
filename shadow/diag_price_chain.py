"""Передача цены по цепочке: хлеб -> мука -> зерно, V3 против pool.

Для мельниц в одном месяце: цена муки, себестоимость, предел закупки зерна,
лучшая landed-цена зерна и свободная касса. Показывает, где цепочка цен
обрывается.
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

from audit.scenario_runner import build_world  # noqa: E402
from modules.economy import Economy  # noqa: E402
from shadow.turn import AgentTurn  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int, default=73129)
    parser.add_argument('--months', type=int, default=36)
    parser.add_argument('--warmup', type=int, default=2)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--modes', default='v3,pool')
    args = parser.parse_args()

    print('PARAMS: seed=%d months=%d warmup=%d workers=%d detail=none'
          % (args.seed, args.months, args.warmup, args.workers))
    for mode in args.modes.split(','):
        work = ROOT / 'tmp' / ('pc-%s-%d' % (mode, args.seed))
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        db, service = build_world(args.seed, work, report_detail='none')
        try:
            for month in range(args.warmup):
                service.advance_turn(command_id='pc-warm-%d' % month, source='audit')
            with ProcessPoolExecutor(max_workers=args.workers) as pool:
                for month in range(args.months):
                    engine = service.economy_state().engine
                    planner = AgentTurn(pool, batch=32) if mode == 'pool' else None
                    engine.step(enterprise_planner=planner)
                    if month == args.months-1:
                        report(engine, planner, mode)
                    service.advance_turn(command_id='pc-%d' % month, source='audit')
        finally:
            db.close()
            shutil.rmtree(work, ignore_errors=True)


def report(engine, planner, mode):
    print()
    print('=== %s, месяц %d' % (mode.upper(), engine.meta['month']))
    wage = engine.cfg['wage']
    for good, input_good in (('flour', 'grain'), ('bread', 'flour')):
        firms = [e for e in engine.enterprises.values() if e['good'] == good]
        prices = sorted(f['price'] for f in firms)
        print('  %-6s цена: мин %.4f медиана %.4f макс %.4f'
              % (good, prices[0], prices[len(prices)//2], prices[-1]))
        traces = []
        if planner is not None:
            for firm in firms[:6]:
                trace = planner.traces.get(firm['id'])
                if trace:
                    traces.append((firm, trace))
        else:
            for firm in firms[:6]:
                traces.append((firm, dict(unit_cost=firm.get('expected_unit_cost', 0.0),
                                          price=firm.get('expected_price', 0.0),
                                          target=(firm.get('production_plan') or {}).get('output', 0.0))))
        if not traces:
            continue
        print('    %-12s %-8s %-9s %-9s %-9s %-10s'
              % ('фирма', 'цена', 'себест.', 'цель', 'касса', 'предел входа'))
        for firm, trace in traces:
            cash = engine.free_cash(firm['id'])
            purchase = (trace.get('purchases') or [[None, 0, 0]])
            bid = purchase[0][2] if purchase and purchase[0][0] is not None else 0.0
            print('    %-12s %-8.4f %-9.4f %-9.1f %-9.1f %-10.4f'
                  % (firm['id'], trace.get('price', 0.0), trace.get('unit_cost', 0.0),
                     trace.get('target', 0.0), cash, bid))
        # Рынок входа.
        sells = [o for o in engine.report['orders']
                 if o['side'] == 'sell' and o['good'] == input_good]
        buys = [o for o in engine.report['orders']
                if o['side'] == 'buy' and o['good'] == input_good
                and o['participant'] in {f['id'] for f in firms}]
        if sells:
            prices_in = sorted(o['price'] for o in sells)
            print('    %s: офферов %d, цены %.4f..%.4f' % (input_good, len(sells),
                                                          prices_in[0], prices_in[-1]))
        if buys:
            print('    заявки мельниц/пекарен: %d на %.1f, исполнено %.1f'
                  % (len(buys), sum(o['quantity'] for o in buys),
                     sum(o['quantity']-o['remaining'] for o in buys)))


if __name__ == '__main__':
    main()
