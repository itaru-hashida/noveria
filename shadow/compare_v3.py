"""Воспроизводимый полный ход V3 / агентов на независимых сохранениях.

python -m shadow.compare_v3 --seed 73129 --months 12 --warmup 2 --workers 4
"""
import argparse
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import tempfile
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor

from audit.scenario_runner import build_world
from modules.economy import Economy
from shadow.turn import AgentTurn
from shadow import sandbox_mp


def worker_ready(ready):
    ready.put(os.getpid())


def warm_worker(_):
    time.sleep(0.02)
    return os.getpid()


def economy_state(engine):
    report = engine.report
    trades = report['trades']
    prices = {}
    for good in engine.goods:
        rows = [t for t in trades if t['good'] == good]
        volume = sum(t['quantity'] for t in rows)
        prices[good] = sum(t['gold']+t['freight'] for t in rows)/volume if volume else None
    return dict(month=engine.meta['month'], production=report['production'],
                consumed=report['consumed'], stocks=engine.total_stock(), prices=prices,
                profit={g: sum(e['profit'] for e in engine.enterprises.values() if e['good'] == g)
                        for g in engine.goods},
                active=report['active_enterprises'],
                opened=[r['enterprise'] for r in report['lifecycle'] if r['action'] == 'opened'],
                idle=dict(Counter(e.get('reason', '') for e in engine.enterprises.values()
                                  if e['status'] == 'idle')),
                orders=len(report['orders']), trades=len(trades),
                invariants=report['invariants'], gold=engine.gold())


def run_mode(args, mode, pool):
    rows = []
    with tempfile.TemporaryDirectory(prefix='noveria-compare-') as directory:
        db, service = build_world(args.seed, Path(directory), report_detail=args.detail)
        try:
            for month in range(args.warmup):
                service.advance_turn(command_id=f'warm-{month}', source='audit')
            captured = {}

            def economy_phase(ctx):
                start = time.perf_counter()
                engine = Economy.load(db)
                load = time.perf_counter()-start
                planner = AgentTurn(pool if mode == 'pool' else None, batch=args.batch) if mode != 'v3' else None
                # Остановка добычи без удаления денег/товаров, затем обычное снятие.
                if args.scenario == 'halt':
                    original_sync = engine.sync_locations

                    def sync_with_halt():
                        original_sync()
                        for firm in engine.enterprises.values():
                            if firm['good'] == 'grain':
                                firm['suspended'] = args.warmup+3 <= engine.meta['month'] <= args.warmup+5
                    engine.sync_locations = sync_with_halt
                report = engine.step(enterprise_planner=planner)
                save_start = time.perf_counter()
                engine.save(db)
                save = time.perf_counter()-save_start
                event_start = time.perf_counter()
                event = db.record_event(service.event('economy_month',
                    dict(month=report['month'], invariants=report['invariants'],
                         trades=len(report['trades']), rules_version=3), [], source='economy',
                    action_id=ctx['command_id'], summary='Экспериментальный месяц экономики'))
                ctx['events'].append(event)
                ctx['changes'].append(f'Экономика: {report["active_enterprises"]} предприятий')
                event_seconds = time.perf_counter()-event_start
                phase = engine.phase_timings
                stream = planner.metrics if planner else {}
                snapshot = stream.get('snapshot', 0.0)
                report_start = time.perf_counter()
                state = economy_state(engine)
                report_seconds = time.perf_counter()-report_start
                captured.update(state, trace=planner.traces if planner else {},
                    stream=stream, exclusive=dict(
                        load_preparation=load+phase['preparation']+snapshot,
                        planning_delivery=phase['planning_execution']-snapshot-stream.get('executor_processing', 0.0),
                        market_execution=phase['market_tail']+stream.get('executor_processing', 0.0),
                        production_settlement=phase['production_settlement'],
                        report_save=phase['report']+save+report_seconds, economy_event=event_seconds))
                count = len(engine.enterprises)
                teardown_started = time.perf_counter()
                if args.scenario == 'halt':
                    engine.sync_locations = original_sync
                    del sync_with_halt, original_sync
                del engine
                captured['exclusive']['report_save'] += time.perf_counter()-teardown_started
                return count

            service.turns.phases = [(name, economy_phase if name == 'economy' else handler)
                                    for name, handler in service.turns.phases]
            for month in range(args.months):
                start = time.perf_counter()
                report = service.advance_turn(command_id=f'{mode}-{month}', source='audit')
                wall = time.perf_counter()-start
                row = dict(captured)
                timings = service.turns.last_timings
                economy_duration = next(p['duration'] for p in report['phases'] if p['name'] == 'economy')
                row['adapter_overhead'] = economy_duration-sum(row['exclusive'].values())
                row['exclusive']['report_save'] += row['adapter_overhead']
                row['exclusive']['load_preparation'] += wall-timings['wall']
                other_phases = sum(p['duration'] for p in report['phases'] if p['name'] != 'economy')
                row['exclusive'] = dict(row['exclusive'], commit_events_framework=
                    timings['commit']+timings['notifications']+timings['orchestration']+other_phases
                    +row['exclusive'].pop('economy_event'))
                row.update(wall=wall, commit=timings['commit'], notifications=timings['notifications'])
                row['unattributed'] = wall-sum(row['exclusive'].values())
                rows.append(row)
                print(mode, row['month'], 'wall', round(wall, 4), 'active', row['active'],
                      'production', {g: round(q, 1) for g, q in row['production'].items()}, flush=True)
        finally:
            db.close()
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int, default=73129)
    parser.add_argument('--months', type=int, default=12)
    parser.add_argument('--warmup', type=int, default=2)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--batch', type=int, default=32)
    parser.add_argument('--detail', choices=['none', 'standard', 'deep'], default='none')
    parser.add_argument('--scenario', choices=['baseline', 'halt'], default='baseline')
    parser.add_argument('--modes', default='v3,inline,pool')
    parser.add_argument('--report', type=Path, default=Path('tmp/full-turn.json'))
    args = parser.parse_args()
    if args.months < 1 or args.workers < 1 or args.batch < 1 or args.warmup < 0:
        parser.error('months, workers, batch > 0; warmup >= 0')
    sandbox_mp.enable_multiprocessing()
    modes = args.modes.split(',')
    if set(modes)-{'v3', 'inline', 'pool'}:
        parser.error('modes: v3,inline,pool')
    result = dict(commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
                  parameters={k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()}, runs={})
    for mode in modes:
        if mode == 'pool':
            start = time.perf_counter()
            context = multiprocessing.get_context('spawn')
            ready = context.Queue()
            with ProcessPoolExecutor(max_workers=args.workers, mp_context=context,
                                     initializer=worker_ready, initargs=(ready,)) as pool:
                list(pool.map(warm_worker, range(args.workers*4)))
                pids = {ready.get(timeout=60) for _ in range(args.workers)}
                result['cold_pool_seconds'] = time.perf_counter()-start
                result['warm_worker_pids'] = sorted(pids)
                result['runs'][mode] = run_mode(args, mode, pool)
            ready.close()
            ready.join_thread()
        else:
            result['runs'][mode] = run_mode(args, mode, None)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    for mode, rows in result['runs'].items():
        print(mode, 'mean wall', sum(r['wall'] for r in rows)/len(rows),
              'max unattributed', max(abs(r['unattributed']) for r in rows))
    print('report:', args.report)


if __name__ == '__main__':
    main()
