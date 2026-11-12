"""Измерить throughput независимых миров для V3 и Cython.

Примеры:

    python audit/native_throughput.py --mode v3 --executor process --workers 4
    python audit/native_throughput.py --mode cython --executor thread --workers 4

Каждая задача создаёт отдельный мир, БД и RNG. Замер шага начинается после
создания мира и warmup, поэтому отдельно видны цена подготовки и скорость
самой симуляции. Это throughput-тест нескольких миров, не параллелизм одного
месяца внутри одного мира.
"""
import argparse
import concurrent.futures
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'audit'))

import scenario_runner as sr


MAPS = {
    'small': (20, 15),
    'standard': (40, 30),
    'large': (80, 60),
}


def build_world(seed, directory, size):
    """Собрать стандартный сценарий с заданным размером генерируемой карты."""
    content = json.loads((ROOT/'content/initial_world.json').read_text(encoding='utf-8'))
    content['generation']['seed'] = seed
    content['generation']['width'], content['generation']['height'] = MAPS[size]
    scenario = Path(directory)/'scenario.json'
    scenario.write_text(json.dumps(content, ensure_ascii=False), encoding='utf-8')
    db = sr.WorldDB(Path(directory)/'world.lbug')
    sr.load_world(db, scenario)
    service = sr.WorldService(db, sr.EventBus())
    service.initialize_economy()
    engine = service.economy_state().engine
    engine.meta['report_detail'] = 'none'
    return db, engine


def signature(engine):
    """Компактная проверка повторяемости одного законченного мира."""
    return {
        'month': engine.meta['month'],
        'rng': repr(engine.rng.getstate()),
        'accounts': tuple((key, row['balance'], row.get('reserved', 0))
                          for key, row in engine.accounts.items()),
        'firms': tuple((key, row['status'], row['price'])
                       for key, row in engine.enterprises.items()),
        'stock': tuple(sorted(engine.total_stock().items())),
        'orders': tuple((row['id'], row['status'], row['remaining'])
                        for row in engine.report['orders']),
        'trades': tuple((row['buy_order'], row['sell_order'], row['quantity'])
                        for row in engine.report['trades']),
    }


def run_task(task):
    seed, size, months, warmup, mode, root = task
    root = Path(root)
    task_dir = Path(tempfile.mkdtemp(prefix=f'native-throughput-{seed}-', dir=root))
    build_started = time.perf_counter()
    db = engine = None
    try:
        db, engine = build_world(seed, task_dir, size)
        build_seconds = time.perf_counter()-build_started
        if mode == 'cython':
            from modules.economy.native import load_serial_step
            load_serial_step()
        native_mode = 'off' if mode == 'v3' else mode
        for _ in range(warmup):
            engine.step(native_mode=native_mode)
        started = time.perf_counter()
        per_month = []
        for _ in range(months):
            month_started = time.perf_counter()
            engine.step(native_mode=native_mode)
            per_month.append(time.perf_counter()-month_started)
            engine.validate()
        run_seconds = time.perf_counter()-started
        return dict(seed=seed, size=size, build_seconds=build_seconds,
                    run_seconds=run_seconds, per_month=per_month,
                    cells=len(engine.tiles), accounts=len(engine.accounts),
                    firms=len(engine.enterprises), signature=signature(engine))
    finally:
        if db is not None:
            db.close()
        shutil.rmtree(task_dir, ignore_errors=True)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=('v3', 'cython'), default='cython')
    parser.add_argument('--executor', choices=('serial', 'thread', 'process'), default='process')
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--months', type=int, default=12)
    parser.add_argument('--warmup', type=int, default=2)
    parser.add_argument('--size', choices=tuple(MAPS), default='standard')
    parser.add_argument('--seeds', type=int, nargs='+',
                        default=(17, 42, 101, 73129, 555, 9001, 12345, 8080))
    parser.add_argument('--work', type=Path, default=ROOT/'tmp'/'native_throughput')
    return parser.parse_args()


def main():
    args = parse_args()
    if args.workers < 1 or args.months < 1 or args.warmup < 0:
        raise SystemExit('workers/months должны быть положительными, warmup — неотрицательным')
    args.work.mkdir(parents=True, exist_ok=True)
    tasks = [(seed, args.size, args.months, args.warmup, args.mode, str(args.work))
             for seed in args.seeds]
    started = time.perf_counter()
    if args.executor == 'serial':
        rows = [run_task(task) for task in tasks]
    else:
        executor = (concurrent.futures.ThreadPoolExecutor
                    if args.executor == 'thread' else concurrent.futures.ProcessPoolExecutor)
        with executor(max_workers=min(args.workers, len(tasks))) as pool:
            rows = list(pool.map(run_task, tasks))
    wall_seconds = time.perf_counter()-started
    simulated_months = len(rows)*args.months
    print(json.dumps({
        'mode': args.mode,
        'executor': args.executor,
        'workers': args.workers,
        'map': args.size,
        'map_size': MAPS[args.size],
        'worlds': len(rows),
        'months_per_world': args.months,
        'wall_seconds_including_world_setup': wall_seconds,
        'throughput_months_per_second_including_world_setup': simulated_months/wall_seconds,
        'mean_world_build_seconds': sum(row['build_seconds'] for row in rows)/len(rows),
        'mean_month_seconds_after_warmup': sum(sum(row['per_month']) for row in rows)/simulated_months,
        'world_shapes': [dict(seed=row['seed'], cells=row['cells'], accounts=row['accounts'],
                              firms=row['firms']) for row in rows],
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
