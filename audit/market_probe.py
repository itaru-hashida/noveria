"""Диагностика рыночной картины: кто, сколько и за какое время её строит.

Зачем: `_market_book`, `landed`, `customer_quotes`, `customer_demand_groups` и
`quoted_cost` обходят одни и те же офферы и спрос по отдельности. Прежде чем
выносить общий `MarketSnapshot`, нужно знать, кто именно платит за эти обходы и
сколько раз кэш уже попадает. Скрипт ничего не меняет в расчёте: он только
оборачивает методы движка счётчиками и возвращает всё как было.

Признак попадания в кэш — тот же самый объект словаря кэша, не выросший в
размере. Так проба не дублирует ключи кэшей (это разошлось бы с движком при
первой же правке) и не может «попасть» вместо промаха после сброса кэша.

Фаза определяется меткой на стеке: обёрнутые фазы хода выставляют её на время
своего выполнения, поэтому вызов из `project_requests` считается инвестиционным,
а из `input_requests` — планированием, без чтения стека `sys._getframe`.

Запуск отдельно:
    python audit/market_probe.py --seed 73129 --months 12 --warmup 2

В составе профиля:
    python audit/profile_run.py --seed 73129 --months 12 --warmup 2 --detail none \
        --no-cprofile --market-probe
"""
import argparse
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.economy import Economy  # noqa: E402

# Фазы хода, к которым относится рыночный вызов. Порядок вложенности важен:
# `project_requests` вызывает планирование не сам, поэтому метка — стек.
PHASES = (
    'sell_orders', 'household_requests', 'owner_requests', 'recover_enterprises',
    'public_project_requests', 'project_requests', 'plan_enterprises',
    'input_requests', 'household_top_up', 'match', 'produce', 'settle',
    'summarize', 'update_prices', 'prepare_recovery_quotes', 'advance_public_projects',
)

# Методы рыночной картины: (имя, как достать товар из аргументов).
TARGETS = {
    '_market_book': lambda self, a, k: a[1],
    'landed': lambda self, a, k: a[2],
    'offers': lambda self, a, k: a[1],
    'quoted_cost': lambda self, a, k: a[1],
    'reference_price': lambda self, a, k: a[1],
    'customer_quotes': lambda self, a, k: a[0].get('good') if a else None,
    'customer_demand_groups': lambda self, a, k: a[0].get('good') if a else None,
    'reachable_demand': lambda self, a, k: a[0].get('good') if a else None,
    'production_outlook': lambda self, a, k: a[0].get('good') if a else None,
    'investment_demand': lambda self, a, k: a[0].get('good') if a else None,
}

# Кэши, к которым обращается метод: по ним считается попадание.
CACHES = {
    '_market_book': '_market_books',
    'landed': '_landed_cache',
    'quoted_cost': '_quoted_cost_cache',
    'reference_price': '_reference_price_cache',
    'customer_quotes': '_customer_quotes_cache',
    'customer_demand_groups': '_customer_demand_rows_cache',
    'production_outlook': '_production_outlook_cache',
    'investment_demand': '_investment_demand_cache',
}

def _cache_state(engine, attribute):
    """Текущий словарь кэша и число записей в нём.

    Держать только длину нельзя: метод мог сбросить кэш и заполнить его заново
    тем же числом записей — это промах, а не попадание. Поэтому запоминается
    и сам объект, и размер на момент входа: кэш мутируется на месте, и без
    снятой копии размера сравнение всегда даёт «равно».
    """
    cache = getattr(engine, attribute, None)
    return cache, (None if cache is None else len(cache))


class MarketProbe:
    """Счётчики вызовов и времени по методам рыночной картины и фазам хода."""

    def __init__(self):
        self.calls = defaultdict(int)
        self.seconds = defaultdict(float)
        self.hits = defaultdict(int)
        self.misses = defaultdict(int)
        self.by_good = defaultdict(lambda: [0, 0.0])
        self.by_phase = defaultdict(lambda: defaultdict(int))
        self.book_builds = defaultdict(int)   # (товар, площадка) → построек
        self.book_options = defaultdict(int)  # (товар, площадка) → офферов в книге
        self.overhead = 0.0
        self._stack = []
        self._originals = {}

    # --- установка ----------------------------------------------------------

    def install(self):
        for name in PHASES:
            func = getattr(Economy, name, None)
            if func is None:
                continue
            self._originals[name] = func
            setattr(Economy, name, self._wrap_phase(func, name))
        for name, good_of in TARGETS.items():
            func = getattr(Economy, name, None)
            if func is None:
                continue
            self._originals[name] = func
            setattr(Economy, name, self._wrap_call(func, name, good_of))
        return self

    def restore(self):
        for name, func in self._originals.items():
            setattr(Economy, name, func)
        self._originals.clear()

    def __enter__(self):
        return self.install()

    def __exit__(self, *exc):
        self.restore()
        return False

    # --- обёртки ------------------------------------------------------------

    def _wrap_phase(self, func, name):
        stack = self._stack

        def wrapper(self, *args, **kwargs):
            stack.append(name)
            try:
                return func(self, *args, **kwargs)
            finally:
                stack.pop()
        return wrapper

    def _wrap_call(self, func, name, good_of):
        calls, seconds = self.calls, self.seconds
        hits, misses = self.hits, self.misses
        by_good, by_phase, stack = self.by_good, self.by_phase, self._stack
        cache_attribute = CACHES.get(name)
        counter = self
        extra = self._note_book if name == '_market_book' else None

        def wrapper(self, *args, **kwargs):
            started = time.perf_counter()
            before = _cache_state(self, cache_attribute) if cache_attribute else None
            try:
                return func(self, *args, **kwargs)
            finally:
                elapsed = time.perf_counter()-started
                counter.overhead += elapsed
                calls[name] += 1
                seconds[name] += elapsed
                if cache_attribute:
                    identity, size = _cache_state(self, cache_attribute)
                    old_identity, old_size = before
                    # Попадание — тот же словарь, не выросший в размере.
                    # Новый объект означает сброс кэша: это промах, даже если
                    # после него записей стало столько же.
                    hit = (identity is old_identity
                           and (old_size is None or size == old_size))
                    if hit:
                        hits[name] += 1
                    else:
                        misses[name] += 1
                        if name == '_market_book' and extra is not None:
                            # Уникальные книги месяца: (товар, площадка
                            # покупателя). Сколько их и сколько раз каждая
                            # строится — это и есть объём общего снимка.
                            extra(self, args)
                try:
                    good = good_of(self, args, kwargs)
                except Exception:
                    good = None
                if good is not None:
                    row = by_good[good]
                    row[0] += 1
                    row[1] += elapsed
                phase = stack[-1] if stack else 'other'
                by_phase[phase][name] += 1
        return wrapper

    # --- отчёт --------------------------------------------------------------

    def _note_book(self, engine, args):
        """Учесть построенную книгу: (товар, площадка покупателя) и её размер.

        Ключ книги в движке — (id(sell_list), площадка, товар). Для снимка нужен
        не id списка, а именно пара «товар × площадка»: это и есть единица
        неизменяемых данных месяца.
        """
        if len(args) < 3:
            return
        buyer, good, sells = args[0], args[1], args[2]
        try:
            tile = engine.market_tile(buyer)
        except Exception:
            return
        try:
            sell_list = sells.get(good, [])
        except Exception:
            return
        entry = getattr(engine, '_market_books', {}).get((id(sell_list), tile, good))
        options = len(entry['options']) if entry else 0
        self.book_builds[(good, tile)] += 1
        self.book_options[(good, tile)] = options

    def report(self, stream=print, top_goods=8):
        total_calls = sum(self.calls.values())
        total_seconds = sum(self.seconds.values())
        stream('=== рыночная картина: вызовы и время ===')
        stream('%-24s %9s %9s %9s %8s %8s' %
               ('метод', 'вызовов', 'секунд', 'мкс/вызов', 'попаданий', 'промахов'))
        for name, count in sorted(self.calls.items(), key=lambda kv: -self.seconds[kv[0]]):
            seconds = self.seconds[name]
            hit = self.hits[name] if name in CACHES else None
            miss = self.misses[name] if name in CACHES else None
            share = (hit/(hit+miss)*100) if hit is not None and hit+miss else None
            stream('%-24s %9d %9.3f %9.1f %8s %8s%s' %
                   (name, count, seconds, seconds/count*1e6,
                    '-' if hit is None else '%d' % hit,
                    '-' if miss is None else '%d' % miss,
                    '' if share is None else '  (%.1f%% попаданий)' % share))
        stream('итого: %d вызовов, %.3f s' % (total_calls, total_seconds))

        stream()
        stream('=== по фазам хода (рыночных вызовов) ===')
        rows = sorted(self.by_phase.items(),
                      key=lambda kv: -sum(self.by_phase[kv[0]].values()))
        for phase, methods in rows:
            stream('%6d  %-22s %s' % (sum(methods.values()), phase,
                   ', '.join('%s=%d' % (k, v) for k, v in
                             sorted(methods.items(), key=lambda kv: -kv[1]))))

        stream()
        stream('=== построение книги и доставка: по товарам ===')
        stream('%10s %9s %9s' % ('вызовов', 'секунд', 'товар'))
        book_goods = defaultdict(int)
        for good, (count, seconds) in sorted(self.by_good.items(),
                                             key=lambda kv: -kv[1][1])[:top_goods]:
            book_goods[good] = count
            stream('%10d %9.3f  %s' % (count, seconds, good))

        if self.book_builds:
            stream()
            stream('=== построенные книги: товар x площадка ===')
            stream('%8s %10s %10s  %s' % ('построек', 'офферов', 'площадка', 'товар'))
            for (good, tile), count in sorted(self.book_builds.items(),
                                              key=lambda kv: -kv[1])[:top_goods]:
                stream('%8d %10d %10s  %s' % (count, self.book_options.get((good, tile), 0),
                                              tile, good))
            stream('уникальных книг за прогон: %d' % len(self.book_builds))
        return dict(calls=dict(self.calls), seconds=dict(self.seconds),
                    hits=dict(self.hits), misses=dict(self.misses),
                    by_good={g: dict(calls=c, seconds=s)
                             for g, (c, s) in self.by_good.items()},
                    by_phase={p: dict(m) for p, m in self.by_phase.items()})


def run(seed, months, warmup, detail='none', stream=print):
    """Прогон фиксированного сценария с пробой. Возвращает отчёт и время хода."""
    import scenario_runner as sr

    workdir = ROOT/'tmp'/('market_probe_%d' % seed)
    import shutil
    if workdir.exists():
        shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)
    db, service = sr.build_world(seed, workdir, report_detail=detail)
    try:
        engine = service.economy_state().engine
        for _ in range(warmup):
            engine.step()
        probe = MarketProbe()
        step_times = []
        with probe:
            for _ in range(months):
                started = time.perf_counter()
                engine.step()
                step_times.append(time.perf_counter()-started)
        mean = sum(step_times)/len(step_times)
        stream()
        stream('ПАРАМЕТРЫ: seed=%d months=%d warmup=%d detail=%s' %
               (seed, months, warmup, detail))
        stream('=== время хода с включённой пробой ===')
        stream('всего %.1fs, в среднем %.3f s/ход (медленнейший %.3f)' %
               (sum(step_times), mean, max(step_times)))
        stream()
        summary = probe.report(stream=stream)
        summary.update(mean_step=mean, month=engine.meta.get('month'),
                       gold=engine.gold(), orders=len(engine.report['orders']),
                       trades=len(engine.report['trades']))
        return summary
    finally:
        db.close()
        shutil.rmtree(workdir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int, default=73129)
    parser.add_argument('--months', type=int, default=12)
    parser.add_argument('--warmup', type=int, default=2)
    parser.add_argument('--detail', choices=['none', 'standard', 'deep'], default='none')
    args = parser.parse_args()
    run(args.seed, args.months, args.warmup, args.detail)


if __name__ == '__main__':
    main()
