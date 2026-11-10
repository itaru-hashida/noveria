"""Что именно связывает каждое звено: сырьё, деньги или труд.

Для муки и хлеба в одном месяце: куплено входа, сколько продукции из него
можно было сделать, сколько сделали, и какой constraint назвал produce().
Отдельно — денежный предел: сколько просила бы фирма без ограничения кассы.
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
    parser.add_argument('--months', type=int, default=10)
    parser.add_argument('--warmup', type=int, default=8)
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()

    work = ROOT / 'tmp' / ('link-%d' % args.seed)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    db, service = sr.build_world(args.seed, work, report_detail='none')
    try:
        for month in range(args.warmup):
            service.advance_turn(command_id='lk-warm-%d' % month, source='audit')

        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            for month in range(args.months):
                engine = Economy.load(db)
                turn = AgentTurn(pool=pool, batch=32)
                bought_before = {a['id']: dict(a['stock']) for a in engine.accounts.values()}
                cash_before = {a['id']: a['balance'] for a in engine.accounts.values()}
                engine.step(enterprise_planner=turn)

                if month < 2:
                    service.advance_turn(command_id='lk-%d' % month, source='audit')
                    continue

                print()
                print('=== месяц %d (seed=%d warmup=%d workers=%d)'
                      % (engine.meta['month'], args.seed, args.warmup, args.workers))
                for good, input_good, ratio in (('flour', 'grain', 1.2), ('bread', 'flour', 1.0)):
                    firms = [e for e in engine.enterprises.values()
                             if e['good'] == good and e['status'] != 'closed']
                    purchased = 0.0
                    for firm in firms:
                        before = bought_before[firm['id']].get(input_good, 0.0)
                        after = engine.accounts[firm['id']]['stock'].get(input_good, 0.0)
                        used = engine.report['inputs_used'].get(input_good, 0.0)
                        purchased += 0.0
                    output = sum(f.get('output', 0.0) for f in firms)
                    planned = sum((f.get('production_plan') or {}).get('output', 0.0) for f in firms)
                    limits = Counter(f.get('production_limit') or '-' for f in firms)
                    money = sum(cash_before[f['id']] for f in firms)
                    cash_left = sum(engine.accounts[f['id']]['balance'] for f in firms)
                    print('  %-6s фирм %3d: план %8.1f выпуск %8.1f | касса до %10.2f после %10.2f'
                          % (good, len(firms), planned, output, money, cash_left))
                    print('         ограничения: %s'
                          % ', '.join('%s=%d' % item for item in limits.most_common(5)))
                # Сколько входа всего куплено и сколько выпущено.
                print('  использовано: grain %.1f, flour %.1f'
                      % (engine.report['inputs_used'].get('grain', 0.0),
                         engine.report['inputs_used'].get('flour', 0.0)))
                print('  произведено: grain %.1f, flour %.1f, bread %.1f'
                      % (engine.report['production'].get('grain', 0.0),
                         engine.report['production'].get('flour', 0.0),
                         engine.report['production'].get('bread', 0.0)))
                service.advance_turn(command_id='lk-%d' % month, source='audit')
    finally:
        db.close()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    main()
