"""Параллельная проверка экономики по многим сидам: отпечатки состояния.

Зачем: один сид ничего не доказывает — траектория хаотична, и правка может
просто не проявиться. Матрица гоняется в отдельных процессах, поэтому проверка
на восьми сидах стоит по времени как на одном.

Почему не `multiprocessing`/`ProcessPoolExecutor`: песочница запрещает
именованные каналы, а они строятся на них (WinError 5 уже на `mp.Pipe`).
Поэтому процессы запускаются как обычные дочерние, а обмен идёт через файлы:
результат — JSON, вывод — лог в `tmp/matrix/logs`.

Отпечаток покрывает счета, предприятия (цены, мощность, история продаж, план),
запасы и состояние RNG: изменение любой величины в расчёте меняет хеш.

Запуск (снять базу, потом сравнить после правки):
    python audit/seed_matrix.py --seeds 17,73129,7,4231 --months 12 --save tmp/base.json
    python audit/seed_matrix.py --seeds 17,73129,7,4231 --months 12 --baseline tmp/base.json
"""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SCRATCH = ROOT / 'tmp' / 'matrix'
LOGS = SCRATCH / 'logs'


def state_digest(engine):
    """sha256 по всему, что определяет траекторию месяца."""
    lines = ['month %d' % engine.meta['month']]
    for account_id in sorted(engine.accounts):
        account = engine.accounts[account_id]
        lines.append('acc %s %s %.9f' % (account_id, account['kind'], account['balance']))
    for firm_id in sorted(engine.enterprises):
        firm = engine.enterprises[firm_id]
        lines.append('firm %s %s %s cap=%.9f price=%.9f plan=%.9f sales=%s' % (
            firm_id, firm['good'], firm['status'], firm['capacity'], firm.get('price', 0.0),
            firm.get('planned_output', 0.0), json.dumps(firm.get('sales_history', []), sort_keys=True)))
    for good, quantity in sorted(engine.total_stock().items()):
        lines.append('stock %s %.9f' % (good, quantity))
    lines.append('gold %.9f' % engine.gold())
    lines.append('rng %r' % (engine.rng.getstate(),))
    return hashlib.sha256('\n'.join(lines).encode('utf-8')).hexdigest()


def run_seed(seed, months, detail):
    """Один сид: свой каталог БД, отпечаток после каждого месяца."""
    import scenario_runner as sr
    from modules.economy import Economy

    directory = SCRATCH / ('seed-%d-%d' % (seed, time.perf_counter_ns() % 1000000))
    directory.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    db, service = sr.build_world(seed, directory, report_detail=detail)
    try:
        digests = {}
        for step in range(1, months + 1):
            service.advance_turn(command_id='matrix-%d-%d' % (seed, step), source='audit')
            engine = service.economy_state().engine
            digests[engine.meta['month']] = state_digest(engine)
        return dict(seed=seed, digests=digests, seconds=round(time.perf_counter() - started, 2))
    finally:
        db.close()
        shutil.rmtree(directory, ignore_errors=True)


def spawn(seed, months, detail, result_path):
    """Дочерний процесс без каналов: вывод в лог, результат в JSON."""
    LOGS.mkdir(parents=True, exist_ok=True)
    log = (LOGS / ('seed-%d.log' % seed)).open('w', encoding='utf-8')
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), '--worker',
         '--seed', str(seed), '--months', str(months), '--detail', detail,
         '--out', str(result_path)],
        cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT)
    return process, log


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seeds', default='17,73129,7,4231')
    parser.add_argument('--months', type=int, default=12)
    parser.add_argument('--detail', default='none')
    parser.add_argument('--save', type=Path, help='куда сохранить отпечатки (база)')
    parser.add_argument('--baseline', type=Path, help='с чем сравнить')
    parser.add_argument('--worker', action='store_true', help='внутренний режим одного сида')
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()

    if args.worker:
        result = run_seed(args.seed, args.months, args.detail)
        args.out.write_text(json.dumps(result), encoding='utf-8')
        return

    seeds = [int(value) for value in args.seeds.split(',') if value.strip()]
    SCRATCH.mkdir(parents=True, exist_ok=True)
    print('ПАРАМЕТРЫ: seeds=%s months=%d detail=%s процессов=%d'
          % (','.join(map(str, seeds)), args.months, args.detail, len(seeds)))

    started = time.perf_counter()
    running = []
    for seed in seeds:
        result_path = SCRATCH / ('result-%d.json' % seed)
        process, log = spawn(seed, args.months, args.detail, result_path)
        running.append((seed, process, log, result_path))
    results = []
    for seed, process, log, result_path in running:
        process.wait()
        log.close()
        if result_path.exists():
            results.append(json.loads(result_path.read_text(encoding='utf-8')))
        else:
            print('seed %d упал, смотри %s' % (seed, LOGS / ('seed-%d.log' % seed)))
    wall = time.perf_counter() - started
    results.sort(key=lambda item: item['seed'])
    if len(results) != len(seeds):
        raise SystemExit('не все сиды посчитаны: %d из %d' % (len(results), len(seeds)))

    print('сидов %d за %.1fs параллельно (последовательно было бы %.1fs)'
          % (len(seeds), wall, sum(item['seconds'] for item in results)))
    for item in results:
        last_month = max(item['digests'])
        print('seed %-6d месяцев=%d  последний хеш %s  (%.2fs)'
              % (item['seed'], len(item['digests']), item['digests'][last_month][:16], item['seconds']))

    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        args.save.write_text(json.dumps({str(item['seed']): item['digests'] for item in results},
                                        indent=2, sort_keys=True), encoding='utf-8')
        print('отпечатки сохранены: %s' % args.save)

    if args.baseline:
        base = json.loads(args.baseline.read_text(encoding='utf-8'))
        mismatches = 0
        for item in results:
            expected = base.get(str(item['seed']), {})
            for month, digest in sorted(item['digests'].items()):
                reference = expected.get(str(month))
                if reference is not None and reference != digest:
                    mismatches += 1
                    print('РАСХОЖДЕНИЕ seed=%d месяц=%s: было %s, стало %s'
                          % (item['seed'], month, reference[:16], digest[:16]))
        total = sum(len(item['digests']) for item in results)
        print('СВЕРКА: расхождений %d из %d месяцев' % (mismatches, total))
        if mismatches:
            raise SystemExit(1)


if __name__ == '__main__':
    main()
