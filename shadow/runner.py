"""Shadow-РїСЂРѕРіРѕРЅ: Р°РіРµРЅС‚РЅР°СЏ РјРѕРґРµР»СЊ СЂСЏРґРѕРј СЃ V3 РЅР° РѕРґРЅРѕРј Рё С‚РѕРј Р¶Рµ РјРёСЂРµ.

РќРёС‡РµРіРѕ РЅРµ РїРёС€РµС‚ РІ Р‘Р” Рё РЅРµ РјРµРЅСЏРµС‚ С‚СЂР°РµРєС‚РѕСЂРёСЋ V3: С‡РёС‚Р°РµС‚ Р¶РёРІРѕР№ РјРёСЂ, СЃС‚СЂРѕРёС‚
РЅРµРёР·РјРµРЅСЏРµРјС‹Р№ СЃРЅРёРјРѕРє, РїР»Р°РЅРёСЂСѓРµС‚ РїСЂРµРґРїСЂРёСЏС‚РёСЏ РїР°СЂР°Р»Р»РµР»СЊРЅРѕ, СЃРІРѕРґРёС‚ Р·Р°СЏРІРєРё С‡РµСЂРµР·
FIFO Рё РїРµС‡Р°С‚Р°РµС‚ РѕС‚С‡С‘С‚.

Р—Р°РїСѓСЃРє:
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

sandbox_mp.enable_multiprocessing()   # РґРѕ Р»СЋР±РѕРіРѕ РїСѓР»Р° Рё РѕС‡РµСЂРµРґРё

from modules.economy import Economy  # noqa: E402
import scenario_runner as sr  # noqa: E402
from shadow import agent_planner, agent_snapshot  # noqa: E402
from shadow.market_executor import Ledger, MarketExecutor
from shadow.producer import Producer  # noqa: E402
from shadow.snapshot_store import SharedSnapshot, read_arrays  # noqa: E402


def plan_batch(job):
    """РџР»Р°РЅ РїР°С‡РєРё Р°РіРµРЅС‚РѕРІ: РѕРґРёРЅ round-trip РїСѓР»Р° РЅР° РјРЅРѕРіРёС… Р°РіРµРЅС‚РѕРІ.

    РџР°С‡РєР° РЅСѓР¶РЅР° РїРѕС‚РѕРјСѓ, С‡С‚Рѕ РїР»Р°РЅ Р°РіРµРЅС‚Р° СЃС‚РѕРёС‚ ~0,1 ms, Р° round-trip Р·Р°РґР°С‡Рё
    РґРѕСЂРѕР¶Рµ: РѕРґРёРЅ Р°РіРµРЅС‚ РЅР° Р·Р°РґР°С‡Сѓ РґР°С‘С‚ 1,06x РЅР° 4860 Р°РіРµРЅС‚Р°С…, РїР°С‡РєР° РїРѕ 32 вЂ” 4,98x.
    """
    name, layout, agents, parameters = job
    # РЁРµСЃС‚РѕР№ СЌР»РµРјРµРЅС‚ (labor_share) РЅСѓР¶РµРЅ РїСЂРѕРёР·РІРѕРґСЃС‚РІСѓ, РїР»Р°РЅРёСЂРѕРІС‰РёРєСѓ РЅРµ РЅСѓР¶РµРЅ.
    plan_parameters = parameters[:5]
    shm, arrays = read_arrays(name, layout)
    try:
        return [(agent, agent_planner.plan(arrays, agent, *plan_parameters))
                for agent in agents]
    finally:
        shm.close()


def prepare_market(engine):
    """РЎС„РѕСЂРјРёСЂРѕРІР°С‚СЊ РєРЅРёРіСѓ РјРµСЃСЏС†Р° РїСЂР°РІРёР»Р°РјРё V3, РЅРµ РґРІРёРіР°СЏ С‚СЂР°РµРєС‚РѕСЂРёСЋ.

    Р—Р°СЏРІРєРё РґРѕРјРѕС…РѕР·СЏР№СЃС‚РІ, СЃРѕР±СЃС‚РІРµРЅРЅРёРєРѕРІ Рё СЃС‚СЂРѕР№РєРё СЂРѕР¶РґР°СЋС‚СЃСЏ РІРЅСѓС‚СЂРё `step()`. Р§С‚РѕР±С‹
    Р°РіРµРЅС‚С‹ РІРёРґРµР»Рё СЂРµР°Р»СЊРЅС‹Р№ СЂС‹РЅРѕРє, С‚Рµ Р¶Рµ С„Р°Р·С‹ РІС‹Р·С‹РІР°СЋС‚СЃСЏ Р·РґРµСЃСЊ РЅР° Р·Р°РіСЂСѓР¶РµРЅРЅРѕРј РІ
    РїР°РјСЏС‚СЊ РґРІРёР¶РєРµ. Р’ Р‘Р” РЅРёС‡РµРіРѕ РЅРµ РїРёС€РµС‚СЃСЏ: `advance_turn` Р·Р°РіСЂСѓР·РёС‚ СЃРѕСЃС‚РѕСЏРЅРёРµ
    Р·Р°РЅРѕРІРѕ.
    """
    engine.update_prices()
    engine.prepare_recovery_quotes()
    sells = engine.sell_orders()
    households = engine.shuffled(a for a in engine.accounts.values()
                                 if a['kind'] == 'household')
    engine.household_requests(households, engine.cfg['basic_needs'], 'Р‘Р°Р·РѕРІС‹Рµ РЅСѓР¶РґС‹', sells)
    engine.household_requests(households, engine.cfg['extra_needs'], 'Р”РѕРїРѕР»РЅРёС‚РµР»СЊРЅС‹Рµ РЅСѓР¶РґС‹', sells)
    engine.owner_requests(sells)
    engine.public_project_requests(sells)
    return sells


def budget_cap(engine):
    """Р’РЅРµС€РЅРёР№ Р»РёРјРёС‚: СЃРІРѕР±РѕРґРЅС‹Рµ РґРµРЅСЊРіРё С„РёСЂРјС‹ РїР»СЋСЃ РґРѕСЃС‚СѓРїРЅРѕРµ С„РёРЅР°РЅСЃРёСЂРѕРІР°РЅРёРµ РІР»Р°РґРµР»СЊС†Р°."""
    return {firm['id']: engine.free_cash(firm['id'])
            for firm in engine.enterprises.values() if firm['status'] != 'closed'}


def run_shadow(engine, parameters, pool, batch=32, submit_order_mode='shuffled'):
    """РћРґРёРЅ РјРµСЃСЏС† Р°РіРµРЅС‚РЅРѕР№ РјРѕРґРµР»Рё РЅР° СЃРЅРёРјРєРµ Р¶РёРІРѕРіРѕ РґРІРёР¶РєР°."""
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
    plans = {}

    def on_done(future):
        """РњРѕРјРµРЅС‚ РіРѕС‚РѕРІРЅРѕСЃС‚Рё worker-Р°: СЃСЂР°Р·Сѓ РІ FIFO, Р±РµР· СЃРѕСЂС‚РёСЂРѕРІРєРё."""
        try:
            planned = future.result()
        except Exception as exc:
            failures.append('%s: %s' % (type(exc).__name__, exc))
            return
        for agent, agent_intents in planned:
            enqueue_order.append(agent)
            planned_output = 0.0
            planned_reason = ''
            for item in agent_intents:
                if item[0] == 'sell':
                    planned_output = item[5]
                    planned_reason = item[6]
                intents.put((agent, item[0], item[1], item[2], item[3], item[4],
                             item[5], item[6]))
            plans[agent] = (planned_output, planned_reason)

    agent_order = list(range(meta['n_agents']))
    if submit_order_mode == 'shuffled':
        # РџРѕРґР°С‡Р° РЅРµ РїРѕ id: РµСЃР»Рё РїРѕСЂСЏРґРѕРє РёСЃРїРѕР»РЅРµРЅРёСЏ РІСЃС‘ СЂР°РІРЅРѕ РёРґС‘С‚ РїРѕ РіРѕС‚РѕРІРЅРѕСЃС‚Рё,
        # Р° РЅРµ РїРѕ РЅРѕРјРµСЂСѓ Р°РіРµРЅС‚Р°, FIFO РґРµР№СЃС‚РІРёС‚РµР»СЊРЅРѕ РѕС‡РµСЂРµРґСЊ.
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

    # Р’С‹РїСѓСЃРє РїРѕСЃР»Рµ РєР»РёСЂРёРЅРіР°, РїРѕ С‚РѕРјСѓ, С‡С‚Рѕ СЂРµР°Р»СЊРЅРѕ РєСѓРїР»РµРЅРѕ. РџРѕСЂСЏРґРѕРє РєР°Рє РІ V3:
    # СЃРЅР°С‡Р°Р»Р° С‚РѕСЂРіРё, РїРѕС‚РѕРј РїСЂРѕРёР·РІРѕРґСЃС‚РІРѕ.
    producer = Producer(ledger, meta, arrays, parameters[1], parameters[5])
    production_rows = producer.run(plans)

    drained = []
    while not outcomes.empty():
        drained.append(outcomes.get())
    snapshot.release()

    money_start = sum(meta['balances'].values())
    money_end = sum(ledger.cash.values())
    stock_start = sum(sum(stock.values()) for stock in meta['all_stock'].values())
    stock_end = sum(sum(stocks.values()) for stocks in ledger.stock.values())

    # РўРѕРІР°СЂРЅС‹Р№ Р±Р°Р»Р°РЅСЃ РїРѕ РєР°Р¶РґРѕРјСѓ С‚РѕРІР°СЂСѓ. РЎРґРµР»РєР° С‚РѕР»СЊРєРѕ РґРІРёРіР°РµС‚ С‚РѕРІР°СЂ, РїРѕСЌС‚РѕРјСѓ
    # РјРёСЂРѕРІРѕР№ РёС‚РѕРі РѕР±СЏР·Р°РЅ СЃРѕС…СЂР°РЅРёС‚СЊСЃСЏ; РІС‹РїСѓСЃРєР° РІ Р°РіРµРЅС‚РЅРѕР№ РјРѕРґРµР»Рё РїРѕРєР° РЅРµС‚.
    goods_start = {}
    for account_id, stocks in meta['all_stock'].items():
        for good, quantity in stocks.items():
            goods_start[good] = goods_start.get(good, 0.0)+quantity
    goods_end = {}
    for account_id, stocks in ledger.stock.items():
        for good, quantity in stocks.items():
            goods_end[good] = goods_end.get(good, 0.0)+quantity
    balance_rows = []
    for good in sorted(set(goods_start) | set(goods_end)):
        start = goods_start.get(good, 0.0)
        made = producer.produced.get(good, 0.0)
        used = producer.consumed.get(good, 0.0)
        end = goods_end.get(good, 0.0)
        expected = start+made-used
        # РЎРґРµР»РєР° С‚РѕРІР°СЂ РЅРµ СЃРѕР·РґР°С‘С‚: РѕСЃС‚Р°С‚РѕРє = РЅР°С‡Р°Р»Рѕ + РІС‹РїСѓСЃРє - РїСЂРѕРёР·РІРѕРґСЃС‚РІРµРЅРЅРѕРµ
        # РїРѕС‚СЂРµР±Р»РµРЅРёРµ. Р’СЃС‘ РѕСЃС‚Р°Р»СЊРЅРѕРµ вЂ” СѓС‚РµС‡РєР° РёР»Рё РґРІРѕР№РЅРѕР№ СѓС‡С‘С‚.
        if abs(end-expected) > 1e-6*max(1.0, abs(expected)):
            balance_rows.append(dict(good=good, start=start, produced=made,
                                     inputs_used=used, end=end,
                                     expected=expected, delta=end-expected))

    return dict(month=meta['month'], meta=meta, ledger=ledger, outcomes=drained,
                failures=failures, snapshot_seconds=snapshot_seconds,
                planning_seconds=planning_seconds, executor=executor,
                snapshot_bytes=size, pool_seconds=pool_seconds,
                money_start=money_start, money_end=money_end,
                production_rows=production_rows,
                produced=dict(producer.produced), consumed=dict(producer.consumed),
                money_ok=abs(money_start-money_end) <= 1e-6*max(1.0, abs(money_start)),
                stock_start=stock_start, stock_end=stock_end,
                no_negative=all(value >= -1e-6 for value in ledger.cash.values()),
                invalid_trades=sum(1 for row in ledger.trades
                                   if row['quantity'] <= 0 or row['price'] <= 0),
                goods_balance=balance_rows,
                enqueue_order=enqueue_order)


def main():
    print('STATIC SHADOW AUDIT: ledger is discarded; for full persistent turns use '
          'python -m shadow.compare_v3')
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int, default=73129)
    parser.add_argument('--months', type=int, default=6)
    parser.add_argument('--warmup', type=int, default=0)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--batch', type=int, default=32)
    parser.add_argument('--detail', default='none')
    parser.add_argument('--report', type=Path, help='РєСѓРґР° СЃРѕС…СЂР°РЅРёС‚СЊ РѕС‚С‡С‘С‚ JSON')
    args = parser.parse_args()

    work = ROOT / 'tmp' / ('shadow-%d' % args.seed)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    db, service = sr.build_world(args.seed, work, report_detail=args.detail)
    # РџСѓР» Р¶РёРІС‘С‚ РІРµСЃСЊ РїСЂРѕРіРѕРЅ: С…РѕР»РѕРґРЅС‹Р№ СЃС‚Р°СЂС‚ spawn+РёРјРїРѕСЂС‚ numpy СЃС‚РѕРёС‚ ~0,5 s Рё РЅР°
    # РјРµСЃСЏС†Рµ РІ 1 s СЃСЉРµРґР°Р» Р±С‹ РІРµСЃСЊ РІС‹РёРіСЂС‹С€, РєРѕС‚РѕСЂРѕРіРѕ РІ РёРіСЂРµ РЅРµС‚.
    runs = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        try:
            for month in range(args.warmup):
                service.advance_turn(command_id='warmup-%d-%d' % (args.seed, month),
                                     source='audit')
            for month in range(args.months):
                engine = service.economy_state().engine
                prepare_market(engine)
                parameters = (engine.cfg['transport_rate'], engine.cfg['wage'],
                              engine.cfg['consumption_tax'], engine.cfg['markup'],
                              engine.cfg.get('enterprise_overhead_rate', 0.0),
                              engine.cfg['labor_share'])
                run = run_shadow(engine, parameters, pool, batch=args.batch)
                runs.append(run)
                print('month %2d: agents %d, intents %d, trades %d, plan %.3fs, '
                      'goods balance %s'
                      % (run['month'], run['meta']['n_agents'], len(run['outcomes']),
                         len(run['ledger'].trades), run['planning_seconds'],
                         'ok' if not run['goods_balance'] else 'BROKEN'))
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
    print('GOODS (all accounts): start %.2f -> end %.2f'
          % (last['stock_start'], last['stock_end']))
    print('negative accounts: %s, invalid trades: %d'
          % ('no' if last['no_negative'] else 'YES', last['invalid_trades']))
    if last['goods_balance']:
        print('GOODS BALANCE BROKEN in %d goods:' % len(last['goods_balance']))
        for row in last['goods_balance'][:5]:
            print('   %-10s start %.2f + made %.2f - used %.2f = %.2f, actual %.2f (delta %+.6f)'
                  % (row['good'], row['start'], row['produced'], row['inputs_used'],
                     row['expected'], row['end'], row['delta']))
    else:
        print('GOODS BALANCE: reconciled for every good')

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

