"""Проба повторных расчётов и кандидатов на кэш в месячном ходе.

Отвечает на вопросы, которые по одному профилю не решаются:
  * сколько раз `production_outlook` считается для одной пары
    (месяц, предприятие) и какое поле ключа кэша мешает попаданию;
  * сколько стоит каждая группа вызовов `recover_enterprises`;
  * даёт ли кэш `customer_quotes` / `market_signal` верный ответ — для этого
    результат сверяется с предыдущим, а не только считается попадание ключа;
  * сколько раз вызывается `market_signal` при скольких разных ключах.

Запуск:
    python audit/outlook_probe.py --seed 73129 --months 16 --warmup 4 --detail none

Скрипт ничего не меняет в движке: он оборачивает методы счётчиками и печатает
статистику. Никакого влияния на расчёт — обёртки вызывают оригинал как есть.
"""
import argparse
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scenario_runner as sr  # noqa: E402
from modules.economy import Economy  # noqa: E402


def caller_name(depth=1):
    try:
        return sys._getframe(depth + 1).f_code.co_name
    except ValueError:
        return '?'


def key_components(engine, e, sells):
    """Скопировать состав ключа кэша production_outlook из v3.py.

    Нужно, чтобы увидеть, какое именно поле меняется между вызовами и мешает
    повторному использованию уже посчитанного плана.
    """
    cacheable = engine.enterprises.get(e.get('id')) is e
    if not cacheable:
        return None
    history = tuple((row.get('sold', 0), row.get('output', 0),
                     bool(row.get('input_limited'))) for row in e.get('sales_history', [])[-3:])
    return dict(sells_id=id(sells), tile=e['tile_id'], price=e['price'],
                production_price=e.get('production_price'), capacity=e['capacity'],
                planned_output=e.get('planned_output', 0),
                stock=engine.accounts[e['id']]['stock'].get(e['good'], 0),
                history=history, demand_revision=engine._demand_revision[e['good']],
                quote_version=getattr(engine, '_market_quote_version', 0),
                trades=len(engine.report['trades']))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int, default=73129)
    parser.add_argument('--months', type=int, default=8)
    parser.add_argument('--warmup', type=int, default=2)
    parser.add_argument('--detail', default='none')
    args = parser.parse_args()

    original_outlook = Economy.production_outlook
    original_quotes = Economy.customer_quotes
    original_recover = Economy.recover_enterprises
    original_signal = Economy.market_signal

    # (month, enterprise) -> список наблюдений за месяц
    outlook_calls = defaultdict(list)
    quotes_calls = Counter()
    recover_calls = Counter()
    recover_time = {}
    memo = {'narrow': dict(calls=0, hits=0, correct=0, last={}, month=None),
            'full': dict(calls=0, hits=0, correct=0, last={}, month=None)}
    quotes_by_source = Counter()
    signals_calls = [0]
    signals_repeat = [0]
    signals_correct = [0]
    signals_wrong = [0]
    signals_month = [None]
    signals_seen = {}

    def recover(self, sells, enterprises=None):
        label = 'полный проход' if enterprises is None else 'одиночное предприятие'
        started = time.perf_counter()
        try:
            return original_recover(self, sells, enterprises)
        finally:
            recover_calls[label] += 1
            recover_time[label] = recover_time.get(label, 0.0) + (time.perf_counter() - started)

    def outlook(self, e, sells=None):
        started = time.perf_counter()
        cache = getattr(self, '_production_outlook_cache', {})
        before = len(cache)
        who = caller_name()
        components = key_components(self, e, sells)
        result = original_outlook(self, e, sells)
        elapsed = time.perf_counter() - started
        after = len(getattr(self, '_production_outlook_cache', {}))
        outlook_calls[(self.meta['month'], e.get('id'))].append(
            dict(caller=who, elapsed=elapsed, miss=after > before,
                 components=components,
                 fingerprint=tuple(sorted((k, v) for k, v in result.items()))))
        return result

    def quotes(self, e, orders=None):
        who = caller_name()
        quotes_calls[who] += 1
        # Сравниваем два возможных ключа мемоизации customer_quotes.
        #   узкий — как у _customer_quotes_cache: без состояния заявок;
        #   полный — плюс всё, что влияет на фильтр и на резервы.
        # Для каждого считаем попадания и сколько из них ВЕРНЫ (отпечаток
        # совпал с последним сохранённым под этим ключом).
        source = getattr(self, '_planning_orders', self.report.get('orders', [])) if orders is None else orders
        base = (id(source), len(source), e['tile_id'], e['good'])
        narrow = base
        full = base + (self._demand_revision[e['good']],
                       getattr(self, '_market_quote_version', 0),
                       len(self.report['trades']))
        result = original_quotes(self, e, orders)
        # customer_quotes возвращает только прошедшие фильтр заявки и их
        # предельную цену. Значит его результат полностью определяется набором
        # (id, price-freight): точные remaining/reserved на ответ не влияют,
        # потому что фильтр смотрит лишь remaining > EPS.
        fingerprint = tuple((o['id'], o['price']-freight) for o, freight in result)
        for label, key in (('narrow', narrow), ('full', full)):
            stat = memo[label]
            # Кэш живёт один отчётный месяц: id() списка переиспользуется
            # сборщиком мусора, и без сброса ключи разных месяцев столкнулись
            # бы, дав ложные «попадания».
            if stat['month'] != self.meta['month']:
                stat['month'] = self.meta['month']
                stat['last'] = {}
            stat['calls'] += 1
            if key in stat['last']:
                stat['hits'] += 1
                if stat['last'][key] == fingerprint:
                    stat['correct'] += 1
                    stat.setdefault('correct_by_caller', Counter())[who] += 1
                else:
                    # Важно, КТО получил устаревший ответ: этим определяется,
                    # можно ли включать кэш только на фазе инвестиций.
                    stat.setdefault('wrong_by_caller', Counter())[who] += 1
            stat['last'][key] = fingerprint
        quotes_by_source[id(source)] += 1
        return result

    def signal(self, history, state, province, good):
        # market_history — чистая функция месяца: список пересобирается в
        # record_market и до этого не меняется. Проверяем кэш так же строго,
        # как это делал бы движок: сверяем САМ объект истории, а не только id,
        # и сбрасываем словарь на смене месяца (id переиспользуется).
        signals_calls[0] += 1
        if signals_month[0] != self.meta['month']:
            signals_month[0] = self.meta['month']
            signals_seen.clear()
        key = (state, province, good)
        entry = signals_seen.get(key)
        result = original_signal(self, history, state, province, good)
        fingerprint = tuple(sorted(result.items()))
        if entry is not None and entry[0] is history:
            signals_repeat[0] += 1
            if entry[1] == fingerprint:
                signals_correct[0] += 1
            else:
                signals_wrong[0] += 1
        signals_seen[key] = (history, fingerprint)
        return result

    Economy.production_outlook = outlook
    Economy.customer_quotes = quotes
    Economy.recover_enterprises = recover
    Economy.market_signal = signal

    # Рабочий каталог внутри проекта: системный %TEMP% недоступен под песочницей,
    # а tmp/ уже исключён из репозитория.
    work = ROOT / 'tmp' / 'outlook_probe'
    work.mkdir(parents=True, exist_ok=True)
    db, service = sr.build_world(args.seed, work, report_detail=args.detail)
    try:
        engine = Economy.load(db)
        engine.meta['report_detail'] = args.detail
        for _ in range(args.warmup):
            engine.step()
        outlook_calls.clear()
        quotes_calls.clear()
        recover_calls.clear()
        recover_time.clear()
        for stat in memo.values():
            stat.update(calls=0, hits=0, correct=0, last={}, month=None)
        quotes_by_source.clear()
        signals_calls[0] = signals_repeat[0] = 0
        signals_correct[0] = signals_wrong[0] = 0
        signals_month[0] = None
        signals_seen.clear()
        t0 = time.perf_counter()
        for _ in range(args.months):
            engine.step()
        wall = time.perf_counter() - t0
        enterprises = len(engine.enterprises)
    finally:
        db.close()

    total = sum(len(v) for v in outlook_calls.values())
    misses = sum(1 for v in outlook_calls.values() for row in v if row['miss'])
    time_total = sum(row['elapsed'] for v in outlook_calls.values() for row in v)
    time_first = sum(row['elapsed'] for v in outlook_calls.values() if v for row in v[:1])
    time_repeat = time_total - time_first

    print('ПАРАМЕТРЫ: seed=%d months=%d warmup=%d detail=%s'
          % (args.seed, args.months, args.warmup, args.detail))
    print('всего %.2fs за %d месяцев, предприятий=%d'
          % (wall, args.months, enterprises))
    print()
    print('=== production_outlook ===')
    print('вызовов=%d  промахов кэша=%d (%.0f%%)' % (total, misses, 100*misses/max(total, 1)))
    print('время внутри вызовов: всего %.2fs, первый вызов на предприятие-месяц %.2fs,'
          ' повторные %.2fs (%.0f%%)'
          % (time_total, time_first, time_repeat, 100*time_repeat/max(time_total, 1e-9)))
    print('в среднем на (месяц, предприятие): %.2f вызова'
          % (total/max(len(outlook_calls), 1)))
    by_caller = Counter(row['caller'] for v in outlook_calls.values() for row in v)
    print('по вызывающим: ' + ', '.join('%s=%d' % kv for kv in by_caller.most_common()))
    for n in (2, 3, 4):
        count = sum(1 for v in outlook_calls.values() if len(v) == n)
        print('  ровно %d вызова: %d пар (месяц, предприятие)' % (n, count))
    multi = sum(1 for v in outlook_calls.values() if len(v) > 1)
    same = sum(1 for v in outlook_calls.values()
               if len(v) > 1 and len({row['fingerprint'] for row in v}) == 1)
    print('  пар с >1 вызовом: %d, из них с полностью одинаковым результатом: %d' % (multi, same))

    differing = Counter()
    identical_key = 0
    for rows in outlook_calls.values():
        if len(rows) < 2 or rows[0]['components'] is None:
            continue
        first, second = rows[0]['components'], rows[-1]['components']
        changed = [k for k, v in first.items() if second.get(k) != v]
        if not changed:
            identical_key += 1
        for key in changed:
            differing[key] += 1
    print('  пар, где ключ кэша не изменился вовсе: %d' % identical_key)
    print('  какие поля ключа расходятся: '
          + ', '.join('%s=%d' % kv for kv in differing.most_common()))

    print()
    print('=== recover_enterprises ===')
    for name, count in recover_calls.most_common():
        print('  %-24s %6d вызовов  %6.2fs (%.0f%% месяца)'
              % (name, count, recover_time.get(name, 0.0),
                 100*recover_time.get(name, 0.0)/max(wall, 1e-9)))

    print()
    print('=== customer_quotes ===')
    total_quotes = sum(quotes_calls.values())
    print('вызовов=%d' % total_quotes)
    for label, title in (('narrow', 'узкий ключ (id, len, tile, good)'),
                         ('full', 'полный (+demand_revision, quote_version, trades)')):
        stat = memo[label]
        wrong = stat['hits']-stat['correct']
        print('  %s: попаданий %d (%.0f%%), из них верных %d, ОШИБОЧНЫХ %d'
              % (title, stat['hits'], 100*stat['hits']/max(total_quotes, 1),
                 stat['correct'], wrong))
        if wrong:
            print('     устаревший ответ получили: '
                  + ', '.join('%s=%d' % kv for kv in
                              stat.get('wrong_by_caller', Counter()).most_common()))
        print('     верные попадания по вызывающим: '
              + ', '.join('%s=%d' % kv for kv in
                          stat.get('correct_by_caller', Counter()).most_common()))
    print('уникальных источников заявок (id списка): %d' % len(quotes_by_source))
    for name, count in quotes_calls.most_common():
        print('  %-28s %8d вызовов' % (name, count))

    print()
    print('=== market_signal ===')
    print('вызовов=%d, повторов по (history, state, province, good)=%d (%.0f%%)'
          % (signals_calls[0], signals_repeat[0],
             100*signals_repeat[0]/max(signals_calls[0], 1)))
    print('из них с тем же результатом: %d, расхождений: %d'
          % (signals_correct[0], signals_wrong[0]))
    print('разных ключей за месяц: %d' % len(signals_seen))


if __name__ == '__main__':
    main()
