"""Почему план маленький: сколько агентов видят спрос и какие у них цены."""
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'audit'))

import scenario_runner as sr  # noqa: E402
from modules.economy import Economy  # noqa: E402
from shadow import agent_planner, agent_snapshot  # noqa: E402
from shadow.runner import budget_cap, prepare_market  # noqa: E402


def main(seed=73129, months=3):
    work = ROOT / 'tmp' / 'diag-plan'
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    db, service = sr.build_world(seed, work, report_detail='none')
    try:
        for month in range(months):
            service.advance_turn(command_id='dp-%d' % month, source='audit')
        engine = service.economy_state().engine
        prepare_market(engine)
        budget = budget_cap(engine)
        arrays, meta = agent_snapshot.build(engine, budget)
        parameters = (engine.cfg['transport_rate'], engine.cfg['wage'],
                      engine.cfg['consumption_tax'], engine.cfg['markup'],
                      engine.cfg.get('enterprise_overhead_rate', 0.0))

        print('PARAMS: seed=%d months=%d detail=none' % (seed, months))
        print('агентов %d, товаров %d, офферов %d, заявок %d'
              % (meta['n_agents'], meta['n_goods'],
                 len(arrays['offer_price']), len(arrays['ask_price'])))

        demand_by_good, plan_by_good, zero_reason = {}, {}, {}
        for agent in range(meta['n_agents']):
            good = int(arrays['agent_good'][agent])
            tile = int(arrays['agent_tile'][agent])
            price = float(arrays['agent_price'][agent])
            demand = agent_planner.reachable_demand(arrays, tile, good, price, parameters[0])
            demand_by_good[meta['goods'][good]] = demand_by_good.get(meta['goods'][good], 0)+demand
            intents = agent_planner.plan(arrays, agent, *parameters)
            sell = [i for i in intents if i[0] == 'sell']
            planned = sell[0][5] if sell else 0.0
            reason = sell[0][6] if sell else 'нет намерения'
            plan_by_good[meta['goods'][good]] = plan_by_good.get(meta['goods'][good], 0)+planned
            if planned <= 0:
                zero_reason[reason] = zero_reason.get(reason, 0)+1

        print('спрос и план по товарам:')
        for good in sorted(demand_by_good):
            print('   %-10s спрос %9.2f план %9.2f' % (good, demand_by_good[good], plan_by_good[good]))
        print('причины нулевого плана:', zero_reason)

        # Цены: сколько агентов дороже публичного максимума спроса.
        asks_by_good = {}
        for index in range(len(arrays['ask_price'])):
            good = int(arrays['ask_good'][index])
            asks_by_good[good] = max(asks_by_good.get(good, 0.0), float(arrays['ask_price'][index]))
        print('максимальные цены спроса:', {meta['goods'][g]: round(p, 2)
                                             for g, p in sorted(asks_by_good.items())})
        prices = {}
        for agent in range(meta['n_agents']):
            good = int(arrays['agent_good'][agent])
            prices.setdefault(meta['goods'][good], []).append(float(arrays['agent_price'][agent]))
        print('цены агентов (min/max):', {g: (round(min(v), 2), round(max(v), 2))
                                          for g, v in sorted(prices.items())})
    finally:
        db.close()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    main()
