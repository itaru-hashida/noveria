"""Что ограничивает выпуск на каждом звене: разбор constraints из produce().

`Economy.produce` пишет `production_observation.constraints` и
`production_limit`. Здесь они собираются по товару, чтобы видеть, какое
ограничение связывает цепочку: мощность, труд, ресурс, сырьё или деньги.
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

from modules.economy import Economy  # noqa: E402
import scenario_runner as sr  # noqa: E402
from shadow.turn import AgentTurn  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int, default=73129)
    parser.add_argument('--months', type=int, default=8)
    parser.add_argument('--warmup', type=int, default=6)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--batch', type=int, default=32)
    args = parser.parse_args()

    work = ROOT / 'tmp' / ('limits-%d' % args.seed)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    db, service = sr.build_world(args.seed, work, report_detail='none')
    try:
        for month in range(args.warmup):
            service.advance_turn(command_id='lim-warm-%d' % month, source='audit')

        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            for month in range(args.months):
                engine = service.economy_state().engine
                turn = AgentTurn(pool=pool, batch=args.batch)
                engine.step(enterprise_planner=turn)

                if month >= args.months-2:
                    print()
                    print('=== месяц %d (workers=%d batch=%d warmup=%d seed=%d)'
                          % (engine.meta['month'], args.workers, args.batch,
                             args.warmup, args.seed))
                    limits = defaultdict(Counter)
                    planned = defaultdict(float)
                    output = defaultdict(float)
                    for firm in engine.enterprises.values():
                        if firm['status'] == 'closed':
                            continue
                        good = firm['good']
                        observation = firm.get('production_observation') or {}
                        planned[good] += (observation.get('procurement_plan') or {}).get('output', 0.0)
                        output[good] += firm.get('output', 0.0)
                        limits[good][firm.get('production_limit') or '-'] += 1
                    for good in ('grain', 'flour', 'bread', 'wood', 'planks', 'tools'):
                        if good not in limits:
                            continue
                        print('  %-7s план %8.1f выпуск %8.1f | %s'
                              % (good, planned[good], output[good],
                                 ', '.join('%s=%d' % item
                                           for item in limits[good].most_common(4))))
                    # Крупнейшие недоборы по сырью.
                    shortfall = []
                    for firm in engine.enterprises.values():
                        if firm['status'] == 'closed':
                            continue
                        observation = firm.get('production_observation') or {}
                        constraints = observation.get('constraints') or {}
                        limit = firm.get('production_limit') or ''
                        if not limit.startswith('Сырьё'):
                            continue
                        planned_output = (observation.get('procurement_plan') or {}).get('output', 0.0)
                        shortfall.append((planned_output-constraints.get(limit, 0.0),
                                          firm['id'], firm['good'], limit,
                                          planned_output, constraints.get(limit, 0.0)))
                    shortfall.sort(reverse=True)
                    for row in shortfall[:6]:
                        print('    недобор %8.1f  %s (%s) %s: план %8.1f, доступно %8.1f'
                              % row)
                service.advance_turn(command_id='lim-%d' % month, source='audit')
    finally:
        db.close()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    main()
