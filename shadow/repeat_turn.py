"""Повторные прогоны полного хода: интервалы, а не одно число.

Каждый режим гоняется несколько раз с одним сидом. Считаются непересекающиеся
интервалы по среднему (минимум-максимум по повторам), занятость исполнителя и
ожидание очереди, отдельно холодный запуск пула.

Запуск:
    python -m shadow.repeat_turn --seed 73129 --months 12 --repeat 3
"""
import argparse
import json
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int, default=73129)
    parser.add_argument('--months', type=int, default=12)
    parser.add_argument('--warmup', type=int, default=2)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--batch', type=int, default=32)
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--modes', default='v3,pool')
    parser.add_argument('--out', type=Path, default=Path('tmp/repeat-turn.json'))
    args = parser.parse_args()

    workdir = ROOT / 'tmp' / 'repeat'
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)

    runs = {}
    for attempt in range(args.repeat):
        out = workdir / ('run-%d.json' % attempt)
        command = [sys.executable, '-m', 'shadow.compare_v3',
                   '--seed', str(args.seed), '--months', str(args.months),
                   '--warmup', str(args.warmup), '--workers', str(args.workers),
                   '--batch', str(args.batch), '--detail', 'none',
                   '--modes', args.modes, '--report', str(out),
                   '--workdir', str(workdir / ('world-%d' % attempt))]
        started = time.perf_counter()
        subprocess.run(command, cwd=str(ROOT), check=True, capture_output=True)
        elapsed = time.perf_counter()-started
        payload = json.loads(out.read_text(encoding='utf-8'))
        print('повтор %d: %.1fs, cold_pool=%s' % (attempt, elapsed,
              round(payload.get('cold_pool_seconds', 0.0), 3)))
        for mode, rows in payload['runs'].items():
            runs.setdefault(mode, []).append(rows)

    print()
    print('PARAMS: seed=%d months=%d warmup=%d workers=%d batch=%d repeat=%d modes=%s'
          % (args.seed, args.months, args.warmup, args.workers, args.batch,
             args.repeat, args.modes))

    summary = {}
    for mode, attempts in runs.items():
        walls = [sum(r['wall'] for r in rows)/len(rows) for rows in attempts]
        summary[mode] = dict(mean=statistics.mean(walls), min=min(walls), max=max(walls),
                             runs=len(attempts))
        print()
        print('%s: средний ход по повторам %s' % (mode, ['%.4f' % w for w in walls]))

    print()
    print('%-28s %-10s %-10s %-10s' % ('блок', 'mean', 'min', 'max'))
    for mode, attempts in runs.items():
        keys = attempts[0][0]['exclusive'].keys()
        print('--- %s' % mode)
        for key in keys:
            values = [sum(r['exclusive'][key] for r in rows)/len(rows) for rows in attempts]
            print('%-28s %-10.5f %-10.5f %-10.5f'
                  % (key, statistics.mean(values), min(values), max(values)))
        wall = [sum(r['wall'] for r in rows)/len(rows) for rows in attempts]
        print('%-28s %-10.5f %-10.5f %-10.5f'
              % ('WALL', statistics.mean(wall), min(wall), max(wall)))

    pool_runs = runs.get('pool')
    if pool_runs:
        print()
        print('--- исполнитель и очередь (pool)')
        for key in ('snapshot', 'stream_wall', 'planning_delivery_inclusive',
                    'drain_after_last', 'executor_processing', 'executor_wait'):
            values = [sum(r['stream'].get(key) or 0.0 for r in rows)/len(rows)
                      for rows in pool_runs]
            print('%-28s mean %.5f  min %.5f  max %.5f'
                  % (key, statistics.mean(values), min(values), max(values)))
        print('перекрытие: stream_wall включает planning и исполнение; '
              'executor_processing не складывается с planning_delivery')

    args.out.write_text(json.dumps({mode: attempts for mode, attempts in runs.items()},
                                   ensure_ascii=False, indent=1, default=str),
                        encoding='utf-8')
    print()
    print('отчёт: %s' % args.out)


if __name__ == '__main__':
    main()
