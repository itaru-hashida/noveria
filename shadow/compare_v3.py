"""Полный новый ход против V3: скорость и экономическое поведение.

ПАРАМЕТРЫ: seed=73129 months=12 detail=none, карта 40x30.
Оба прогона начинаются с одного мира; V3 крутится в своей БД, агентная модель
читает тот же мир и не пишет в него.
"""
import argparse
import shutil
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'audit'))

from shadow import sandbox_mp  # noqa: E402

sandbox_mp.enable_multiprocessing()

from modules.economy import Economy  # noqa: E402
import scenario_runner as sr  # noqa: E402
from shadow.runner import prepare_market, run_shadow  # noqa: E402


def economy_state(engine):
    """Показатели поведения, которые требует задача 3."""
    total_demand = 0.0
    met_demand = 0.0
    deficits = 0.0
    for account in engine.accounts.values():
        if account['kind'] != 'household':
            continue
        for need in account.get('needs', {}).values():
            total_demand += need.get('needed', 0.0)
            met_demand += need.get('consumed', 0.0)
            deficits += need.get('deficit', 0.0)
    active = sum(1 for e in engine.enterprises.values() if e['status'] == 'active')
    idle = sum(1 for e in engine.enterprises.values() if e['status'] == 'idle')
    closed = sum(1 for e in engine.enterprises.values() if e['status'] == 'closed')
    output = sum(e.get('output', 0.0) for e in engine.enterprises.values())
    prices = {}
    for entry in engine.report.get('prices', {}).items():
        prices[entry[0]] = entry[1]
    return dict(households_needed=total_demand, households_consumed=met_demand,
                households_deficit=deficits,
                fulfilment=met_demand/total_demand if total_demand else 0.0,
                active=active, idle=idle, closed=closed, output=output,
                firms=len(engine.enterprises), gold=engine.gold())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int, default=73129)
    parser.add_argument('--months', type=int, default=12)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--warmup', type=int, default=2)
    args = parser.parse_args()

    work = ROOT / 'tmp' / ('compare-%d' % args.seed)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    db, service = sr.build_world(args.seed, work, report_detail='none')
    try:
        for month in range(args.warmup):
            service.advance_turn(command_id='cmp-warm-%d' % month, source='audit')

        print('PARAMS: seed=%d months=%d warmup=%d detail=none workers=%d'
              % (args.seed, args.months, args.warmup, args.workers))

        # --- V3 -----------------------------------------------------------
        v3_states = []
        v3_started = time.perf_counter()
        for month in range(args.months):
            service.advance_turn(command_id='cmp-v3-%d' % month, source='audit')
            engine = Economy.load(db)
            v3_states.append(economy_state(engine))
        v3_seconds = time.perf_counter()-v3_started

        # --- агентная модель на свежем мире --------------------------------
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        db.close()
        db, service = sr.build_world(args.seed, work, report_detail='none')
        for month in range(args.warmup):
            service.advance_turn(command_id='cmp-warm2-%d' % month, source='audit')

        shadow_states = []
        balance_broken = 0
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            shadow_started = time.perf_counter()
            for month in range(args.months):
                engine = Economy.load(db)
                prepare_market(engine)
                parameters = (engine.cfg['transport_rate'], engine.cfg['wage'],
                              engine.cfg['consumption_tax'], engine.cfg['markup'],
                              engine.cfg.get('enterprise_overhead_rate', 0.0),
                              engine.cfg['labor_share'])
                run = run_shadow(engine, parameters, pool, batch=32)
                if run['goods_balance']:
                    balance_broken += 1
                shadow_states.append(dict(
                    output=sum(r['output'] for r in run['production_rows']),
                    active=sum(1 for r in run['production_rows'] if r['output'] > 0),
                    idle=sum(1 for r in run['production_rows'] if r['output'] <= 0),
                    intents=len(run['outcomes']),
                    filled=sum(1 for row in run['outcomes'] if row['filled'] > 0),
                    gold=run['money_end']))
                # Месяц V3 вперёд, чтобы мир жил: агенты читают следующий мир.
                service.advance_turn(command_id='cmp-shadow-%d' % month, source='audit')
            shadow_seconds = time.perf_counter()-shadow_started

        print()
        print('3) ПОЛНЫЙ ХОД: агентная модель против V3')
        print('   V3:      %.2fs за %d месяцев (%.3f s/ход)'
              % (v3_seconds, args.months, v3_seconds/args.months))
        print('   агенты:  %.2fs за %d месяцев (%.3f s/ход, только план+клиринг+выпуск)'
              % (shadow_seconds, args.months, shadow_seconds/args.months))
        print()
        print('   поведение (последний месяц)      V3        агенты')
        v3_last, sh_last = v3_states[-1], shadow_states[-1]
        print('   выпуск фирм                   %10.2f  %10.2f'
              % (v3_last['output'], sh_last['output']))
        print('   активных / простаивающих      %4d/%-5d %4d/%-5d'
              % (v3_last['active'], v3_last['idle'], sh_last['active'], sh_last['idle']))
        print('   выполнено заявок               %10s  %10d из %d'
              % ('-', sh_last['filled'], sh_last['intents']))
        print('   удовлетворение потребления     %9.1f%%  %9s'
              % (100*v3_last['fulfilment'], '-'))
        print('   золото в мире                 %10.2f  %10.2f'
              % (v3_last['gold'], sh_last['gold']))
        print()
        print('   месяцев с нарушенным балансом товара: %d' % balance_broken)
        print('   выпуск по месяцам (агенты): %s'
              % ', '.join('%.1f' % s['output'] for s in shadow_states))
        print('   выпуск по месяцам (V3):     %s'
              % ', '.join('%.1f' % s['output'] for s in v3_states))
    finally:
        db.close()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    main()
