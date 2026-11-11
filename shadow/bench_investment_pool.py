"""Speculative project-candidate pool on one frozen investment state.

This is a ceiling probe, not an alternative game turn: workers do not apply
projects, and their RNG/caches are independent of the parent.
"""
from concurrent.futures import ProcessPoolExecutor
import argparse
import copy
from multiprocessing import get_context
from pathlib import Path
from time import perf_counter
import pickle
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'audit'))
from audit import scenario_runner as sr  # noqa: E402
from audit.profile_run import workdir  # noqa: E402
from modules.economy import Economy  # noqa: E402


class Captured(Exception):
    pass


_WORKER_STATE = None


def init_worker(snapshot, allowed_goods):
    global _WORKER_STATE
    engine, sells = pickle.loads(snapshot)
    _WORKER_STATE = (engine, sells, allowed_goods)


def evaluate_batch(ids):
    engine, sells, allowed_goods = _WORKER_STATE
    return [(account_id, engine.project_candidate(engine.accounts[account_id], sells,
              allowed_goods=allowed_goods)) for account_id in ids]


def evaluate_snapshot_job(job):
    snapshot, ids, allowed_goods = job
    init_worker(snapshot, allowed_goods)
    return evaluate_batch(ids)


def ping():
    return 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--compact', action='store_true')
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()
    with workdir('investment_pool_probe') as tmp:
        db, service = sr.build_world(73129, tmp, report_detail='none')
        original = Economy.project_candidate
        capture = {}

        def stop_at_first(self, investor, sells, *args, **kwargs):
            capture['engine'] = self
            capture['sells'] = sells
            capture['allowed_goods'] = kwargs.get('allowed_goods')
            raise Captured

        try:
            engine = service.economy_state().engine
            for _ in range(2):
                engine.step()
            Economy.project_candidate = stop_at_first
            try:
                engine.step()
            except Captured:
                pass
        finally:
            Economy.project_candidate = original
            db.close()

        if not capture:
            raise RuntimeError('no investment review captured')
        started = perf_counter()
        snapshot_engine = copy.copy(capture['engine'])
        if args.compact:
            snapshot_engine.paths = {}
            for key in ('_market_books', '_customer_demand_rows_cache',
                        '_landed_cache', '_customer_quotes_cache', '_customer_order_index'):
                snapshot_engine.__dict__.pop(key, None)
        snapshot = pickle.dumps((snapshot_engine, capture['sells']), protocol=5)
        packed = perf_counter()-started
        field_sizes = []
        for key, value in capture['engine'].__dict__.items():
            try:
                field_sizes.append((len(pickle.dumps(value, protocol=5)), key))
            except (TypeError, AttributeError):
                pass
        print('largest_fields=' + repr(sorted(field_sizes, reverse=True)[:8]))
        accounts = capture['engine'].accounts
        enterprises = capture['engine'].enterprises
        month = capture['engine'].meta['month']
        ids = [a['id'] for a in accounts.values()
               if a['kind'] in ('private', 'merchant', 'treasury', 'municipal') or
               (a['kind'] == 'enterprise' and enterprises[a['id']]['status'] != 'closed'
                and enterprises[a['id']]['available_from'] <= month)]
        jobs = [ids[i:i+32] for i in range(0, len(ids), 32)]
        init_worker(snapshot, capture['allowed_goods'])
        started = perf_counter()
        inline = [row for job in jobs for row in evaluate_batch(job)]
        inline_time = perf_counter()-started
        context = get_context('spawn')
        with ProcessPoolExecutor(max_workers=args.workers, mp_context=context,
                                 initializer=init_worker,
                                 initargs=(snapshot, capture['allowed_goods'])) as pool:
            started = perf_counter()
            parallel = [row for batch in pool.map(evaluate_batch, jobs) for row in batch]
            pool_time = perf_counter()-started
        with ProcessPoolExecutor(max_workers=args.workers, mp_context=context) as pool:
            [future.result() for future in (pool.submit(ping) for _ in range(args.workers))]
            size = (len(ids)+args.workers-1)//args.workers
            streamed_jobs = [(snapshot, ids[i:i+size], capture['allowed_goods'])
                             for i in range(0, len(ids), size)]
            warm_pool_times = []
            for _ in range(2):
                started = perf_counter()
                streamed = [row for batch in pool.map(evaluate_snapshot_job, streamed_jobs)
                            for row in batch]
                warm_pool_times.append(perf_counter()-started)
            paired = []
            whole_job = (snapshot, ids, capture['allowed_goods'])
            for repeat in range(6):
                order = ('inline', 'pool') if repeat % 2 == 0 else ('pool', 'inline')
                times = {}
                for mode in order:
                    started = perf_counter()
                    if mode == 'inline':
                        evaluate_snapshot_job(whole_job)
                    else:
                        list(pool.map(evaluate_snapshot_job, streamed_jobs))
                    times[mode] = perf_counter()-started
                paired.append(times['pool']-times['inline'])
        mismatches = sum(a != b for a, b in zip(inline, parallel))
        streamed_mismatches = sum(a != b for a, b in zip(inline, streamed))
        print(f'accounts={len(ids)} batches={len(jobs)} snapshot_bytes={len(snapshot)} '
              f'pickle_seconds={packed:.4f}')
        print(f'inline_seconds={inline_time:.4f} pool_seconds={pool_time:.4f} '
              f'candidate_mismatches={mismatches}')
        print(f'warm_pool_seconds={warm_pool_times} '
              f'candidate_mismatches={streamed_mismatches}')
        print(f'paired_pool_minus_inline={paired} mean={sum(paired)/len(paired):.4f}')


if __name__ == '__main__':
    main()
