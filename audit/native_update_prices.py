"""Точность и стоимость native prices + recovery + sell_orders без cProfile.

    python audit/native_update_prices.py --seed 73129 --months 12
    python audit/native_update_prices.py --seed 73129 --benchmark --abba 4
"""
import argparse
import copy
import shutil
import statistics
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'audit'))

import scenario_runner as sr
from modules.economy.native.update_prices_boundary import run_native_market


def assert_same(reference, native, month):
    for name in ('accounts', 'enterprises', 'meta', 'report'):
        left, right = getattr(reference, name), getattr(native, name)
        if left != right:
            if isinstance(left, dict):
                for key in left.keys() | right.keys():
                    if left.get(key) != right.get(key):
                        raise AssertionError(f'месяц {month}: {name}.{key}')
            raise AssertionError(f'месяц {month}: {name}')
    if reference._recovery_quotes != native._recovery_quotes:
        raise AssertionError(f'месяц {month}: recovery quotes')
    for name in ('_market_quote_version', '_market_quote_versions', '_market_quotes_epoch'):
        if getattr(reference, name) != getattr(native, name):
            raise AssertionError(f'месяц {month}: {name}')
    if reference.rng.getstate() != native.rng.getstate():
        raise AssertionError(f'месяц {month}: RNG')
    reference.validate()
    native.validate()


def verify(engine, months):
    reference, native = copy.deepcopy(engine), copy.deepcopy(engine)
    for month in range(1, months+1):
        # Отдельная проверка границы на одном и том же входном состоянии.
        direct_reference, direct_native = copy.deepcopy(reference), copy.deepcopy(reference)
        direct_reference.update_prices()
        direct_reference.prepare_recovery_quotes()
        reference_sells = direct_reference.sell_orders()
        run_native_market(direct_native)
        assert_same(direct_reference, direct_native, month)
        if reference_sells != direct_native.native_market['sells']:
            raise AssertionError(f'месяц {month}: sells')
        reference.step()
        native.step(native_mode='cython')
        assert_same(reference, native, month)
        if month == 1 or month == months:
            print(f'проверка: месяц {month}, совпадение, '
                  f'заявок={len(reference.report["orders"])}, сделок={len(reference.report["trades"])}')


def measured_v3(checkpoint):
    engine = copy.deepcopy(checkpoint)
    original_update = engine.update_prices
    original_recovery = engine.prepare_recovery_quotes
    original_sell = engine.sell_orders
    elapsed = []
    def timed_update(self):
        started = time.perf_counter()
        result = original_update()
        elapsed.append(time.perf_counter()-started)
        return result
    def timed_recovery(self):
        started = time.perf_counter()
        result = original_recovery()
        elapsed.append(time.perf_counter()-started)
        return result
    def timed_sell(self, exclude=()):
        started = time.perf_counter()
        result = original_sell(exclude=exclude)
        elapsed.append(time.perf_counter()-started)
        return result
    engine.update_prices = types.MethodType(timed_update, engine)
    engine.prepare_recovery_quotes = types.MethodType(timed_recovery, engine)
    engine.sell_orders = types.MethodType(timed_sell, engine)
    started = time.perf_counter()
    engine.step()
    return time.perf_counter()-started, sum(elapsed)


def measured_native(checkpoint):
    engine = copy.deepcopy(checkpoint)
    started = time.perf_counter()
    engine.step(native_mode='cython')
    wall = time.perf_counter()-started
    timings = engine.native_update_prices['timings']
    return wall, timings


def benchmark(engine, pairs):
    # Прогрев checkpoint не входит ни в один замер.
    checkpoint = copy.deepcopy(engine)
    rows = {'v3_step': [], 'v3_market_phases': [], 'native_step': [],
            'pack': [], 'native': [], 'apply': []}
    for pair in range(pairs):
        for mode in ('v3', 'native', 'native', 'v3'):
            if mode == 'v3':
                wall, market_phases = measured_v3(checkpoint)
                rows['v3_step'].append(wall)
                rows['v3_market_phases'].append(market_phases)
            else:
                wall, timings = measured_native(checkpoint)
                rows['native_step'].append(wall)
                for key in ('pack', 'native', 'apply'):
                    rows[key].append(timings[key])
        print(f'ABBA: пара {pair+1}/{pairs} завершена')
    for key, values in rows.items():
        print(f'{key:12s}: медиана {statistics.median(values)*1000:.3f} мс, '
              f'диапазон {min(values)*1000:.3f}..{max(values)*1000:.3f} мс')
    delta = statistics.median(rows['v3_step'])-statistics.median(rows['native_step'])
    print(f'полный step V3 − native: {delta*1000:.3f} мс по медианам')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int, default=73129)
    parser.add_argument('--months', type=int, default=12)
    parser.add_argument('--warmup', type=int, default=2)
    parser.add_argument('--benchmark', action='store_true')
    parser.add_argument('--abba', type=int, default=4)
    args = parser.parse_args()
    work = ROOT/'tmp'/'native_update_prices'
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    db, service = sr.build_world(args.seed, work, report_detail='none')
    try:
        engine = service.economy_state().engine
        for _ in range(args.warmup):
            engine.step()
        if args.benchmark:
            benchmark(engine, args.abba)
        else:
            verify(engine, args.months)
    finally:
        db.close()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    main()
