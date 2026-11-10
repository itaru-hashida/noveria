"""Замер стоимости хранения экономики на пути настоящего хода.

Мерит ход через `WorldService.advance_turn` — то есть полный путь игры: фазы
хода, чтение мира и запись экономики. Скрипт совместим с обоими устройствами
хранения, чтобы «было/стало» мерилось одной командой:

    python audit/storage_bench.py --seed 73129 --months 12 --detail none
    python audit/storage_bench.py --seed 73129 --months 12 --detail none --autosave

До миграции движок читался `Economy.load(db)`, после — берётся из
`service.economy_state()`. Отпечаток состояния считается по счетам,
предприятиям и RNG; он же доказывает, что хранение не изменило расчёт.
"""
import argparse
import hashlib
import json
import shutil
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scenario_runner as sr  # noqa: E402
from modules.economy import Economy  # noqa: E402


def current_engine(service):
    state_getter = getattr(service, 'economy_state', None)
    if state_getter is not None:
        return state_getter().engine
    return Economy.load(service.db)


def fingerprint(engine):
    payload = dict(
        month=engine.meta['month'],
        gold=engine.gold(),
        accounts={a['id']: [repr(a['balance']),
                            {g: repr(q) for g, q in sorted(a['stock'].items())},
                            {g: repr(q) for g, q in sorted(a.get('values', {}).items())}]
                  for a in engine.accounts.values()},
        firms={e['id']: [e['status'], repr(e['output']), repr(e['price']),
                         repr(e.get('sales_forecast', 0.0)),
                         [repr(h.get('sold', 0)) for h in e.get('sales_history', [])]]
               for e in engine.enterprises.values()},
        rng=repr(engine.rng.getstate()),
    )
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int, default=73129)
    parser.add_argument('--months', type=int, default=12)
    parser.add_argument('--detail', default='none')
    parser.add_argument('--autosave', action='store_true',
                        help='включить autosave каждого хода (по умолчанию off)')
    parser.add_argument('--out', type=Path, default=ROOT / 'tmp' / 'storage_bench')
    args = parser.parse_args()

    shutil.rmtree(args.out, ignore_errors=True)
    args.out.mkdir(parents=True, exist_ok=True)
    db, service = sr.build_world(args.seed, args.out, report_detail=args.detail)
    times, marks = [], []
    try:
        if args.autosave:
            service.autosave = 'turn'
        for step in range(1, args.months + 1):
            started = time.perf_counter()
            service.advance_turn(command_id=f'bench-{step}', source='bench')
            times.append(time.perf_counter() - started)
            marks.append(fingerprint(current_engine(service)))
        total = sum(times)
        print('ПАРАМЕТРЫ: seed=%d months=%d detail=%s autosave=%s'
              % (args.seed, args.months, args.detail, getattr(service, 'autosave', 'off')))
        print('всего %.2fs, среднее %.3f s/ход, медиана %.3f s/ход, разброс %.3f–%.3f'
              % (total, total / len(times), statistics.median(times), min(times), max(times)))
        print('первые %.3f, последние %.3f s/ход'
              % (statistics.mean(times[:3]), statistics.mean(times[-3:])))
        print('отпечаток состояния:', marks[-1])
        if marks[0] == marks[-1]:
            raise SystemExit('отпечаток не меняется между ходами — замер бессмыслен')
    finally:
        db.close()
        print('каталог мира: %s' % args.out)


if __name__ == '__main__':
    main()
