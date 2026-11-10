"""Shadow-прогон: агентная модель рядом с V3 на одном и том же мире.

Ничего не пишет в БД и не меняет траекторию V3: читает живой мир, строит
неизменяемый снимок, планирует предприятия параллельно, сводит заявки через
FIFO и печатает отчёт.

Запуск:
    python -m shadow.runner --seed 73129 --months 12 --workers 4
"""
import argparse
import json
import queue
import random
import shutil
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'audit'))

from shadow import sandbox_mp  # noqa: E402

sandbox_mp.enable_multiprocessing()   # до любого пула и очереди

from modules.economy import Economy  # noqa: E402
import scenario_runner as sr  # noqa: E402
from shadow import agent_planner, agent_snapshot  # noqa: E402
from shadow.market_executor import Ledger, MarketExecutor  # noqa: E402
from shadow.snapshot_store import SharedSnapshot, read_arrays  # noqa: E402


def plan_batch(job):
    """План пачки агентов: один round-trip пула на многих агентов.

    Пачка нужна потому, что план агента стоит ~0,1 ms, а round-trip задачи
    дороже: один агент на задачу даёт 1,06x на 4860 агентах, пачка по 32 — 4,98x.
    """
    name, layout, agents, parameters = job
    shm, arrays = read_arrays(name, layout)
    try:
        return [(agent, agent_planner.plan(arrays, agent, *parameters)) for agent in agents]
    finally:
        shm.close()


def prepare_market(engine):
    """Сформировать книгу месяца правилами V3, не двигая траекторию.

    Заявки домохозяйств, собственников и стройки рождаются внутри `step()`. Чтобы
    агенты видели реальный рынок, те же фазы вызываются здесь на загруженном в
    память движке. В БД ничего не пишется: `advance_turn` загрузит состояние
    заново.
    """
    engine.update_prices()
    engine.prepare_recovery_quotes()
    sells = engine.sell_orders()
    households = engine.shuffled(a for a in engine.accounts.values()
                                 if a['kind'] == 'household')
    engine.household_requests(households, engine.cfg['basic_needs'], 'Базовые нужды', sells)
    engine.household_requests(households, engine.cfg['extra_needs'], 'Дополнительные нужды', sells)
    engine.owner_requests(sells)
    engine.public_project_requests(sells)
    return sells


def budget_cap(engine):
    """Внешний лимит: свободные деньги фирмы плюс доступное финансирование владельца."""
    result = {}
    for firm in engine.enterprises.values():
        if firm['status'] == 'closed':
            continue
        limit = engine.free_cash(firm['id'])
        if engine.accounts.get(firm['owner_id']) is not None:
            limit += max(0.0, engine.cash_after_commitments(firm['owner_id'], 0.0))
        result[firm['id']] = limit
    return result


def run_shadow(engine, parameters, pool, batch=32, submit_order_mode='shuffled'):
    """Один месяц агентной модели на снимке живого движка."""
    budget = budget_cap(engine)
    arrays, meta = agent_snapshot.build(engine, budget)
    layout, size = agent_snapshot.describe(arrays)

    started = time.perf_counter()
    snapshot = SharedSnapshot.create(arrays, layout, size)
    snapshot_seconds = time.perf_counter()-started

    ledger = Ledger(meta, arrays)
    intents = queue.Queue()
    outcomes = queue.Queue()
    books = {'distance': {row: {column: float(arrays['distance'][row, column])
                                for column in range(meta['n_tiles'])}
                          for row in range(meta['n_tiles'])}}
    weights = {index: float(arrays['good_weight'][index]) for index in range(meta['n_goods'])}
    executor = MarketExecutor(ledger, intents, books, meta, outcomes, weights, parameters[0])
    executor.start()

    failures = []
    enqueue_order = []

    def on_done(future):
        """Момент готовности worker-а: сразу в FIFO, без сортировки."""
        try:
            planned = future.result()
        except Exception as exc:
            failures.append('%s: %s' % (type(exc).__name__, exc))
            return
        for agent, agent_intents in planned:
            enqueue_order.append(agent)
            for item in agent_intents:
                intents.put((agent, item[0], item[1], item[2], item[3], item[4],
                             item[5], item[6]))

    agent_order = list(range(meta['n_agents']))
    if submit_order_mode == 'shuffled':
        # Подача не по id: если порядок исполнения всё равно идёт по готовности,
        # а не по номеру агента, FIFO действительно очередь.
        random.Random(73129).shuffle(agent_order)
    groups = [agent_order[index:index+batch]
              for index in range(0, len(agent_order), batch)]

    started = time.perf_counter()
    pool_started = time.perf_counter()
    futures = [pool.submit(plan_batch, (snapshot.name, layout, group, parameters))
               for group in groups]
    for future in futures:
        future.add_done_callback(on_done)
    for future in futures:
        future.result(timeout=600)
    pool_seconds = time.perf_counter()-pool_started
    planning_seconds = time.perf_counter()-started

    executor.phase_done.set()
    executor.join(timeout=60)

    drained = []
    while not outcomes.empty():
        drained.append(outcomes.get())
    snapshot.release()

    money_start = sum(meta['balances'].values())
    money_end = sum(ledger.cash.values())
    stock_start = sum(sum(stock.values()) for stock in meta['start_stock'].values())
    stock_end = sum(sum(stock.values()) for stock in ledger.stock.values())

    return dict(month=meta['month'], meta=meta, ledger=ledger, outcomes=drained,
                failures=failures, snapshot_seconds=snapshot_seconds,
                planning_seconds=planning_seconds, executor=executor,
                snapshot_bytes=size, pool_seconds=pool_seconds,
                money_start=money_start, money_end=money_end,
                money_ok=abs(money_start-money_end) <= 1e-6*max(1.0, abs(money_start)),
                stock_start=stock_start, stock_end=stock_end,
                no_negative=all(value >= -1e-6 for value in ledger.cash.values()),
                invalid_trades=sum(1 for row in ledger.trades
                                   if row['quantity'] <= 0 or row['price'] <= 0),
                enqueue_order=enqueue_order)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int, default=73129)
    parser.add_argument('--months', type=int, default=6)
    parser.add_argument('--warmup', type=int, default=0)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--batch', type=int, default=32)
    parser.add_argument('--detail', default='none')
    parser.add_argument('--report', type=Path, help='куда сохранить отчёт JSON')
    args = parser.parse_args()

    work = ROOT / 'tmp' / ('shadow-%d' % args.seed)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    db, service = sr.build_world(args.seed, work, report_detail=args.detail)
    # Пул живёт весь прогон: холодный старт spawn+импорт numpy стоит ~0,5 s и на
    # месяце в 1 s съедал бы весь выигрыш, которого в игре нет.
    runs = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        try:
            for month in range(args.warmup):
                service.advance_turn(command_id='warmup-%d-%d' % (args.seed, month),
                                     source='audit')
            for month in range(args.months):
                engine = Economy.load(db)
                prepare_market(engine)
                parameters = (engine.cfg['transport_rate'], engine.cfg['wage'],
                              engine.cfg['consumption_tax'], engine.cfg['markup'],
                              engine.cfg.get('enterprise_overhead_rate', 0.0))
                run = run_shadow(engine, parameters, pool, batch=args.batch)
                runs.append(run)
                print('month %2d: agents %d, intents %d, trades %d, plan %.3fs'
                      % (run['month'], run['meta']['n_agents'], len(run['outcomes']),
                         len(run['ledger'].trades), run['planning_seconds']))
                service.advance_turn(command_id='shadow-%d-%d' % (args.seed, month),
                                     source='audit')
        finally:
            db.close()
            shutil.rmtree(work, ignore_errors=True)

    last = runs[-1]
    print()
    print('PARAMS: seed=%d months=%d warmup=%d workers=%d batch=%d detail=%s'
          % (args.seed, args.months, args.warmup, args.workers, args.batch, args.detail))
    print('snapshot: %d agents, %.1f KB, build+write %.3fs'
          % (last['meta']['n_agents'], last['snapshot_bytes']/1024,
             last['snapshot_seconds']))
    print('plan+clear month: %.3fs (pool itself %.3fs)'
          % (last['planning_seconds'], last['pool_seconds']))
    print('intents %d, trades %d, worker failures %d'
          % (len(last['outcomes']), len(last['ledger'].trades), len(last['failures'])))
    print('MONEY: start %.2f -> end %.2f (balanced %s)'
          % (last['money_start'], last['money_end'], last['money_ok']))
    print('GOODS (firm stock): start %.2f -> end %.2f'
          % (last['stock_start'], last['stock_end']))
    print('negative accounts: %s, invalid trades: %d'
          % ('no' if last['no_negative'] else 'YES', last['invalid_trades']))

    reasons = {}
    for row in last['outcomes']:
        reasons[row['reason']] = reasons.get(row['reason'], 0)+1
    print('reasons: %s' % ', '.join('%s=%d' % item for item in
                                    sorted(reasons.items(), key=lambda x: -x[1])[:6]))
    executed = sum(1 for row in last['outcomes'] if row['filled'] > 0)
    print('filled orders: %d of %d' % (executed, len(last['outcomes'])))

    order = last['enqueue_order']
    unique = []
    for agent in order:
        if not unique or unique[-1] != agent:
            unique.append(agent)
    print('enqueue order (first 18): %s' % unique[:18])
    print('equals sorted by id: %s -> %s'
          % (unique == sorted(unique),
             'canonical order, not readiness' if unique == sorted(unique)
             else 'real readiness order'))
    sequences = [row['sequence'] for row in last['outcomes']]
    gaps = [value for index, value in enumerate(sequences)
            if index and value != sequences[index-1]+1]
    print('executor sequence strictly FIFO: %s (gaps %d)' % (not gaps, len(gaps)))

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        payload = [dict(month=run['month'], outcomes=run['outcomes'],
                        trades=run['ledger'].trades, failures=run['failures'],
                        planning_seconds=run['planning_seconds'],
                        pool_seconds=run['pool_seconds'],
                        snapshot_seconds=run['snapshot_seconds'],
                        snapshot_bytes=run['snapshot_bytes'],
                        money_start=run['money_start'], money_end=run['money_end'],
                        enqueue_order=run['enqueue_order']) for run in runs]
        args.report.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                               encoding='utf-8')
        print('report: %s' % args.report)


if __name__ == '__main__':
    main()
