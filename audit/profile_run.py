"""Профилирование месячного хода экономики на реальном мире.

Запуск:
    python audit/profile_run.py --seed 73129 --months 16
    python audit/profile_run.py --seed 73129 --months 16 --detail standard
    python audit/profile_run.py --seed 73129 --months 16 --warmup 4 --top 30

Отчёт печатается в stdout: время хода по фазам (tottime) и накопительное
(cumtime). Скрипт ничего не меняет в движке — только наблюдает.
"""
import argparse
import cProfile
import io
import pstats
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scenario_runner as sr  # noqa: E402
from modules.economy import Economy  # noqa: E402


# Функции, входящие в месячный ход. Замеряем их отдельно, чтобы видеть
# стоимость фазы целиком (cumtime), а не только собственный код.
PHASE_FUNCTIONS = (
    'activate_rules', 'sync_locations', 'update_prices', 'prepare_recovery_quotes',
    'sell_orders', 'household_requests', 'owner_requests', 'recover_enterprises',
    'public_project_requests', 'project_requests', 'input_requests', 'match',
    'record_market', 'produce', 'finish_projects', 'consume_owner_purchases',
    'settle', 'pay_dividends', 'summarize', 'validate',
    'build_causal_events',
)


def install_phase_timers():
    """Обернуть фазы хода счётчиками времени и вызовов."""
    totals = {}
    counts = {}
    original = {}

    for name in PHASE_FUNCTIONS:
        func = getattr(Economy, name, None)
        if func is None:
            continue
        original[name] = func

        def make(fn, key):
            def wrapper(self, *args, **kwargs):
                started = time.perf_counter()
                try:
                    return fn(self, *args, **kwargs)
                finally:
                    totals[key] = totals.get(key, 0.0) + (time.perf_counter() - started)
                    counts[key] = counts.get(key, 0) + 1
            return wrapper

        setattr(Economy, name, make(func, name))

    def restore():
        for name, func in original.items():
            setattr(Economy, name, func)

    return totals, counts, restore


def warm_caches(engine, months):
    """Разогрев: первый ход строит маршруты и граф рецептов, его время
    не показательно для установившегося режима."""
    for _ in range(months):
        engine.step()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int, default=73129)
    parser.add_argument('--months', type=int, default=16)
    parser.add_argument('--warmup', type=int, default=0,
                        help='сколько месяцев прогреть до замера (вне профиля)')
    parser.add_argument('--detail', choices=['none', 'standard', 'deep'], default=None,
                        help='режим отчётности движка; None — как в конфиге')
    parser.add_argument('--top', type=int, default=25)
    parser.add_argument('--out', type=Path, default=ROOT / 'audit/_runs')
    args = parser.parse_args()

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        db, service = sr.build_world(args.seed, tmp,
                                     report_detail=args.detail or 'deep')
        try:
            engine = Economy.load(db)
            if args.detail:
                engine.meta['report_detail'] = args.detail

            if args.warmup:
                t0 = time.perf_counter()
                warm_caches(engine, args.warmup)
                print('прогрев %d мес: %.2fs' % (args.warmup, time.perf_counter() - t0))

            totals, counts, restore = install_phase_timers()
            profiler = cProfile.Profile()
            step_times = []
            t_all = time.perf_counter()
            for i in range(args.months):
                started = time.perf_counter()
                profiler.enable()
                report = engine.step()
                profiler.disable()
                step_times.append(time.perf_counter() - started)
                if i in (0, args.months - 1):
                    print('месяц %3d: %.2fs  заявок=%d  сделок=%d'
                          % (report['month'], step_times[-1],
                             len(report['orders']), len(report['trades'])), flush=True)
            wall = time.perf_counter() - t_all
            restore()

            mean = sum(step_times) / len(step_times)
            print()
            print('=== ИТОГ: %d месяцев, seed %d, detail=%s ==='
                  % (args.months, args.seed, args.detail or 'deep'))
            print('всего %.1fs, в среднем %.3f s/ход' % (wall, mean))
            print('быстрейший %.2fs, медленнейший %.2fs, разброс %.2fx'
                  % (min(step_times), max(step_times),
                     max(step_times) / max(min(step_times), 1e-9)))

            print()
            print('=== по фазам хода (накопительно, включая вложенные вызовы) ===')
            print('%10s %8s %8s  %s' % ('секунд', 'доля', 'вызовов', 'фаза'))
            for name, seconds in sorted(totals.items(), key=lambda kv: -kv[1]):
                print('%10.2f %7.1f%% %8d  %s'
                      % (seconds, 100 * seconds / wall, counts[name], name))

            stream = io.StringIO()
            stats = pstats.Stats(profiler, stream=stream).sort_stats('tottime')
            stats.print_stats(args.top)
            print()
            print('=== профиль по функциям (tottime, собственный код) ===')
            for line in stream.getvalue().splitlines():
                # оставляем только строки данных, отбрасывая шапку pstats
                if 'function calls' in line or 'Ordered by' in line:
                    continue
                if line.strip().startswith(('ncalls', 'List reduced')):
                    continue
                if line.strip():
                    print(line)

            args.out.mkdir(parents=True, exist_ok=True)
            target = args.out / ('profile_%d_%dmonths.txt' % (args.seed, args.months))
            stats.dump_stats(str(args.out / ('profile_%d_%dmonths.prof'
                                             % (args.seed, args.months))))
            print()
            print('профиль сохранён: %s' % (args.out / ('profile_%d_%dmonths.prof'
                                                       % (args.seed, args.months))))
        finally:
            db.close()


if __name__ == '__main__':
    main()
