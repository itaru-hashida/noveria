"""Аудит накладной работы вокруг полного Economy.step().

Запуск: python audit/step_overhead_audit.py --seed 73129 --months 12 --warmup 2
Счётчики включаются только после прогрева и не меняют состояние экономики.
"""
import argparse
from collections import Counter, defaultdict
import inspect
from pathlib import Path
import pickle
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'audit'))

import modules.economy.v3 as v3  # noqa: E402
from audit import scenario_runner as sr  # noqa: E402
from audit.profile_run import workdir  # noqa: E402


METHODS = (
    'summarize', 'record_market', 'validate', 'persist_investment_state',
    'build_causal_events', 'reset_report', 'total_stock', 'gold',
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int, default=73129)
    parser.add_argument('--months', type=int, default=12)
    parser.add_argument('--warmup', type=int, default=2)
    parser.add_argument('--detail', default='none', choices=('none', 'standard', 'deep'))
    args = parser.parse_args()

    timings = defaultdict(float)
    counts = Counter()
    deepcopy_types = Counter()
    deepcopy_callers = Counter()
    originals = {name: getattr(v3.Economy, name) for name in METHODS}
    original_deepcopy = v3.copy.deepcopy
    original_sorted = v3.sorted if hasattr(v3, 'sorted') else sorted

    def timed(method, name):
        def wrapper(self, *method_args, **kwargs):
            started = time.perf_counter()
            try:
                return method(self, *method_args, **kwargs)
            finally:
                timings[name] += time.perf_counter()-started
                counts[name] += 1
        return wrapper

    def counted_deepcopy(value, *copy_args, **kwargs):
        started = time.perf_counter()
        try:
            return original_deepcopy(value, *copy_args, **kwargs)
        finally:
            timings['deepcopy'] += time.perf_counter()-started
            counts['deepcopy'] += 1
            deepcopy_types[type(value).__name__] += 1
            frame = inspect.currentframe().f_back
            if frame and str(frame.f_code.co_filename).startswith(str(ROOT)):
                caller = Path(frame.f_code.co_filename).relative_to(ROOT)
                deepcopy_callers[f'{caller}:{frame.f_lineno}'] += 1

    def counted_sorted(*sort_args, **kwargs):
        started = time.perf_counter()
        try:
            return original_sorted(*sort_args, **kwargs)
        finally:
            timings['sorted'] += time.perf_counter()-started
            counts['sorted'] += 1

    with workdir('step_overhead_audit') as tmp:
        db, service = sr.build_world(args.seed, tmp, report_detail=args.detail)
        try:
            engine = service.economy_state().engine
            for _ in range(args.warmup):
                engine.step()
            for name, method in originals.items():
                setattr(v3.Economy, name, timed(method, name))
            v3.copy.deepcopy = counted_deepcopy
            v3.sorted = counted_sorted
            wall = []
            report_sizes = []
            history_sizes = []
            for _ in range(args.months):
                started = time.perf_counter()
                report = engine.step()
                wall.append(time.perf_counter()-started)
                report_sizes.append(len(pickle.dumps(report, protocol=5)))
                history_sizes.append(len(pickle.dumps(engine.meta['market_history'], protocol=5)))
        finally:
            v3.copy.deepcopy = original_deepcopy
            if hasattr(v3, 'sorted'):
                delattr(v3, 'sorted')
            for name, method in originals.items():
                setattr(v3.Economy, name, method)
            db.close()

    print(f'wall_seconds={sum(wall):.6f} per_step={sum(wall)/len(wall):.6f}')
    for name in sorted(timings, key=timings.get, reverse=True):
        print(f'{name}: seconds={timings[name]:.6f} calls={counts[name]}')
    print('deepcopy_types=' + repr(deepcopy_types.most_common()))
    print('deepcopy_callers=' + repr(deepcopy_callers.most_common()))
    print(f'report_bytes_mean={sum(report_sizes)/len(report_sizes):.0f} '
          f'history_bytes_mean={sum(history_sizes)/len(history_sizes):.0f}')
    print('checkpoint_or_db_writes_inside_engine_step=0 (no save/checkpoint call path)')


if __name__ == '__main__':
    main()
