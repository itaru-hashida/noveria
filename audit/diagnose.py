"""Диагностика причин дефицита и простоя на отдельных месяцах (код движка не меняется).

Запуск: python audit/diagnose.py --seed 17 --months 36 --at 6,12,24,36 --scenario baseline
Пишет audit/_runs/diag_<scenario>_<seed>.json
"""
import argparse
import json
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'audit'))

import scenario_runner as sr  # noqa: E402
from modules.economy import Economy  # noqa: E402


def snapshot(engine, report):
    out = {}
    firms = list(engine.enterprises.values())
    by_good = defaultdict(lambda: Counter())
    for e in firms:
        by_good[e['good']][(e['status'], e.get('production_limit') or e.get('reason') or '')] += 1
    out['firms_by_good'] = {g: {f'{s}|{r}': n for (s, r), n in c.items()} for g, c in by_good.items()}
    out['capacity_vs_output'] = {g: dict(capacity=round(sum(e['capacity'] for e in firms if e['good'] == g and e['status'] != 'closed'), 1),
                                         output=round(sum(e['output'] for e in firms if e['good'] == g), 1))
                                 for g in engine.goods}
    fd = report.get('food_diagnostics', {})
    out['food_totals'] = {g: {k: round(v, 1) for k, v in t.items()} for g, t in fd.get('totals', {}).items()}
    # распределение денег домохозяйств (по душе)
    hh = [(a['balance']/max(engine.population(a), 1), engine.population(a), a['province_id'], a['id']) for a in engine.accounts.values() if a['kind'] == 'household']
    hh.sort()
    total_pop = sum(p for _, p, _, _ in hh)
    acc, q = 0, {}
    for per, p, _, _ in hh:
        acc += p
        for pct in (10, 25, 50, 75, 90):
            if pct not in q and acc >= total_pop*pct/100:
                q[pct] = round(per, 3)
    out['household_gold_per_capita_quantiles'] = q
    out['household_min_max'] = (round(hh[0][0], 4), round(hh[-1][0], 4))
    # причины нереализованных базовых заявок
    reasons = Counter()
    unmet = defaultdict(float)
    for o in report['orders']:
        if o['side'] == 'buy' and o['purpose'] == 'Базовые нужды' and not o.get('superseded_by_top_up') and o['remaining'] > 1e-7:
            code = o.get('match_observation', {}).get('reason_code', '?')
            reasons[(o['good'], code)] += 1
            unmet[o['good']] += o['remaining']
    out['basic_unfilled_reasons'] = {f'{g}|{c}': n for (g, c), n in reasons.items()}
    out['basic_unfilled_quantity'] = {g: round(v, 1) for g, v in unmet.items()}
    # входные заявки производителей, которые не исполнились
    ir = Counter()
    for o in report['orders']:
        if o['side'] == 'buy' and o['purpose'] == 'Сырьё' and o['remaining'] > 1e-7:
            ir[(o['good'], o.get('match_observation', {}).get('reason_code', '?'))] += 1
    out['input_unfilled_reasons'] = {f'{g}|{c}': n for (g, c), n in ir.items()}
    input_gaps = defaultdict(lambda: dict(orders=0, quantity=0.0,
                                          cheapest_landed=None, highest_limit=0.0))
    for o in report['orders']:
        if o['side'] != 'buy' or o['purpose'] != 'Сырьё' or o['good'] not in ('grain', 'flour') or o['remaining'] <= 1e-7:
            continue
        observation = o.get('match_observation', {})
        key = f"{o['good']}|{observation.get('reason_code', '?')}"
        row = input_gaps[key]
        row['orders'] += 1
        row['quantity'] += o['remaining']
        row['highest_limit'] = max(row['highest_limit'], o['price'])
        for candidate in observation.get('candidates', []):
            if candidate.get('route_available') and candidate.get('remaining_after', 0) > 1e-7:
                price = candidate['landed_price']
                row['cheapest_landed'] = min(row['cheapest_landed'], price) if row['cheapest_landed'] is not None else price
    out['input_gaps'] = {key: {name: round(value, 2) if isinstance(value, float) else value
                               for name, value in row.items()} for key, row in input_gaps.items()}
    flour_price_gaps = []
    for o in report['orders']:
        if (o['side'] != 'buy' or o['purpose'] != 'Сырьё' or o['good'] != 'flour'
                or o['remaining'] <= 1e-7
                or o.get('match_observation', {}).get('reason_code') != 'landed_price_above_limit'):
            continue
        offers = [c for c in o['match_observation']['candidates']
                  if c['route_available'] and c.get('remaining_after', 0) > 1e-7]
        if not offers:
            continue
        cheapest = min(offers, key=lambda c: c['landed_price'])
        firm = engine.enterprises.get(o['participant'], {})
        plan = firm.get('production_plan', {})
        ratios = plan.get('input_ratios', {})
        other_cost = plan.get('wage_unit', 0) + sum(
            ratio*plan.get('input_prices', {}).get(good, 0)
            for good, ratio in ratios.items() if good != 'flour')
        profitable_ceiling = ((plan.get('expected_net_price', 0)-other_cost)/ratios['flour']
                              if ratios.get('flour', 0) else None)
        flour_price_gaps.append(dict(buyer=o['participant'], province=o['province_id'],
            remaining=round(o['remaining'], 2), bid=round(o['price'], 4),
            seller=cheapest['seller'], sell_order=cheapest['sell_order'],
            seller_price=round(cheapest['price'], 4),
            freight=round(cheapest['freight_per_unit'], 4),
            landed=round(cheapest['landed_price'], 4),
            gap=round(cheapest['landed_price']-o['price'], 4),
            distance=round(cheapest['distance'], 2),
            seller_remaining=round(cheapest['remaining_after'], 2),
            expected_margin=round(firm.get('expected_margin', 0), 4),
            profitable_ceiling=round(profitable_ceiling, 4) if profitable_ceiling is not None else None,
            planned_output=round(firm.get('planned_output', 0), 2)))
    out['flour_price_gaps'] = flour_price_gaps
    # нереализованные запасы у продавцов
    sells = [o for o in report['orders'] if o['side'] == 'sell' and o['remaining'] > 1e-7]
    sg = defaultdict(float)
    for o in sells:
        sg[o['good']] += o['remaining']
    out['unsold_offers'] = {g: round(v, 1) for g, v in sg.items()}
    out['stocks'] = {g: round(v, 1) for g, v in report['stocks'].items()}
    # строительство
    out['projects'] = [dict(investor=a['id'], good=a['project']['good'], created=a['project']['created'],
                            last_progress=a['project'].get('last_progress'), capacity=round(a['project']['capacity'], 1),
                            working_cash=round(a['project']['working_cash'], 1))
                       for a in engine.accounts.values() if a.get('project')]
    out['owner_cash'] = {a['id']: round(a['balance'], 1) for a in engine.accounts.values() if a['kind'] in ('private', 'merchant', 'treasury')}
    out['closed_firms'] = sum(e['status'] == 'closed' for e in firms)
    # кто фактически получил товар: сделки по видам покупателя
    kind = {a['id']: a['kind'] for a in engine.accounts.values()}
    bought = defaultdict(lambda: defaultdict(float))
    for t in report['trades']:
        bought[t['good']][kind[t['buyer']]+('/'+t['purpose'] if kind[t['buyer']] != 'household' else '')] += t['quantity']
    out['bought_by_buyer'] = {g: {k: round(v, 1) for k, v in d.items()} for g, d in bought.items() if g in ('bread', 'fish', 'cloth', 'flour', 'grain')}
    out['owner_consumed'] = {g: round(v, 1) for g, v in report.get('owner_consumed', {}).items()}
    out['owner_budget'] = round(sum(r['budget'] for r in report.get('owner_consumption', [])), 1)
    out['owner_spent'] = round(sum(r['spent'] for r in report.get('owner_consumption', [])), 1)
    out['household_consumed'] = {g: round(v, 1) for g, v in report['consumed'].items()}
    # подробности по фирмам цепочки хлеба
    rows = []
    for e in firms:
        if e['good'] not in ('bread', 'flour', 'grain', 'fish'):
            continue
        a = engine.accounts[e['id']]
        rows.append(dict(id=e['id'], good=e['good'], status=e['status'], prov=e['province_id'], cap=round(e['capacity'], 1),
                         out=round(e['output'], 1), stock=round(a['stock'].get(e['good'], 0), 1), price=round(e['price'], 3),
                         forecast=round(e.get('sales_forecast', 0), 1), demand=round(e.get('reachable_demand', 0), 1),
                         margin=round(e.get('expected_margin', 0), 3), limit=e.get('production_limit'),
                         cash=round(a['balance'], 1), plan=round(e.get('planned_output', 0), 1),
                         hist=[round(h['sold'], 1) for h in e.get('sales_history', [])], opened=e['opened']))
    out['firms'] = rows
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--seed', type=int, required=True)
    p.add_argument('--months', type=int, default=36)
    p.add_argument('--at', default='6,12,24,36')
    p.add_argument('--scenario', default='baseline', choices=['baseline', 'route_cut', 'halt'])
    p.add_argument('--out', type=Path, default=ROOT/'audit/_runs')
    a = p.parse_args()
    at = {int(x) for x in a.at.split(',')}
    sr.install_shock()
    shock = None if a.scenario == 'baseline' else (a.scenario, 'auto', 7, 12)
    sr.SHOCK.update(kind=shock[0] if shock else None, province=None, start=7, end=12)
    result = {}
    with tempfile.TemporaryDirectory() as tmp:
        db, service = sr.build_world(a.seed, tmp)
        try:
            engine = Economy.load(db)
            if shock:
                sr.SHOCK['province'] = sr.choose_province(engine)
                result['shock_province'] = sr.SHOCK['province']
            for step in range(1, a.months+1):
                service.advance_turn(command_id=f'diag-{a.scenario}-{a.seed}-{step}', source='audit')
                if step in at:
                    engine = Economy.load(db)
                    report = db.economy_month(engine.meta['month'])
                    result[str(step)] = snapshot(engine, report)
                    print('diag month', step, flush=True)
        finally:
            db.close()
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out/f'diag_{a.scenario}_{a.seed}.json').write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding='utf-8')


if __name__ == '__main__':
    main()
